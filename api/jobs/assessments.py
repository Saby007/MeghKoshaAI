"""Supervise one isolated assessment subprocess per worker replica."""

import asyncio
import logging
import os
import sys
import time
from tempfile import TemporaryDirectory

import asyncpg
from azure.core.exceptions import AzureError

from services import assessment_store
from services.assessment_settings import scalable_mode, settings

logger = logging.getLogger(__name__)


async def supervise(job: asyncpg.Record) -> None:
    with TemporaryDirectory(prefix="meghkosha-assessment-") as directory:
        await _supervise_in_workspace(job, directory)


async def _supervise_in_workspace(job: asyncpg.Record, directory: str) -> None:
    job_id, token = job["job_id"], job["lease_token"]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "jobs.assessment_task", str(job_id), str(token),
        env={**os.environ, "ASSESSMENT_WORKER_PROCESS": "true", "ASSESSMENT_WORKSPACE": directory},
    )
    started = time.monotonic()
    try:
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=settings().lease_seconds / 3)
            except TimeoutError:
                if time.monotonic() - started > settings().timeout_seconds:
                    await assessment_store.fail(
                        job_id, token, "Assessment execution time limit exceeded. Narrow the scope before retrying.",
                        retryable=False, code="execution_limit",
                    )
                    break
                if not await assessment_store.heartbeat(job_id, token):
                    logger.info("Stopping assessment %s after cancellation or lease loss", job_id)
                    break
        if process.returncode is not None and process.returncode != 0:
            await assessment_store.fail(
                job_id, token, "Assessment worker exited unexpectedly. Review worker logs and memory capacity.",
                retryable=True, code="worker_exit",
            )
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()


async def run() -> None:
    if not scalable_mode():
        raise RuntimeError("The assessment worker requires ASSESSMENT_MODE=postgres")
    try:
        while True:
            try:
                job = await assessment_store.claim()
                if job:
                    await supervise(job)
                else:
                    await asyncio.sleep(settings().poll_seconds)
            except (asyncpg.PostgresError, AzureError, ConnectionError, OSError, TimeoutError):
                logger.exception("Assessment worker cannot reach a required service")
                await asyncio.sleep(settings().poll_seconds)
    finally:
        await assessment_store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
