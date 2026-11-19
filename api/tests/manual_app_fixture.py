"""Loopback-only real SQL/manual-upload fixture; never included in the application image."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import asyncpg
import uvicorn

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import main
from jobs import assessment_task
from services import assessment_store, focus_uploads, report_snapshots
from test_manual_postgres import MemoryStorage

storage = MemoryStorage()


@asynccontextmanager
async def lifespan(app):
    dsn = os.environ["ASSESSMENT_TEST_DSN"]
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        raise RuntimeError("The browser fixture requires the dedicated loopback synthetic database.")
    os.environ["ASSESSMENT_MODE"] = "postgres"
    os.environ["ASSESSMENT_WORKER_PROCESS"] = "true"
    os.environ["WORKSPACE_COOKIE_SECURE"] = "false"
    db = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    if not await db.fetchval("SELECT to_regclass('assessments.workspaces')"):
        await db.execute((ROOT / "services" / "assessment_schema.sql").read_text())

    async def pool(**kwargs):
        return db

    async def close():
        return None

    async def archive(snapshot, **kwargs):
        return None

    @asynccontextmanager
    async def container():
        yield storage

    async def worker():
        while True:
            job = await assessment_store.claim()
            if job is None:
                await asyncio.sleep(0.2)
                continue
            with TemporaryDirectory(prefix="manual-browser-") as directory:
                os.environ["ASSESSMENT_WORKSPACE"] = directory
                await assessment_task.run(job["job_id"], job["lease_token"])

    assessment_store.pool = pool
    assessment_store.close = close
    report_snapshots.publish_snapshot = archive
    focus_uploads.container = container
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await db.close()


main.app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8002, log_level="warning", http="h11", ws="none")
