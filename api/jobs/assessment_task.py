"""Build and atomically publish a leased assessment outside the API process."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException

from services import assessment_store, report_snapshots, target_connection
from services.focus_cost_reader import FocusCostDataError
from services.assessment_limits import AssessmentBudget, AssessmentLimitError, active_budget
from services.assessment_settings import settings

logger = logging.getLogger(__name__)


async def run(job_id: UUID, token: UUID) -> None:
    os.environ["ASSESSMENT_WORKER_PROCESS"] = "true"
    try:
        job = await (await assessment_store.pool()).fetchrow(
            "SELECT * FROM assessments.jobs WHERE job_id=$1 AND lease_token=$2 "
            "AND status='running' AND lease_until>now()", job_id, token,
        )
        if not job:
            raise assessment_store.LeaseLostError("Assessment lease is no longer active.")
        import json
        workspace_id = UUID(job["tenant_id"])
        if not await (await assessment_store.pool()).fetchval(
            "SELECT 1 FROM assessments.workspaces WHERE workspace_id=$1 AND expires_at>now()", workspace_id,
        ):
            raise ValueError("The uploaded-report workspace expired or is unavailable.")

        directory = Path(os.environ["ASSESSMENT_WORKSPACE"])
        if not directory.is_dir():
            raise RuntimeError("The supervisor-owned assessment workspace is unavailable.")
        budget_token = active_budget.set(AssessmentBudget(settings(), directory))
        try:
            payload = json.loads(job["task_payload"])
            async with target_connection.context(str(workspace_id), payload.get("configurationRevision")):
                if job.get("task_kind", "assessment") == "artifact":
                    from jobs.report_artifact import run as build_artifact
                    await build_artifact(job)
                    return
                from services.manual_assessment import build
                await (await assessment_store.pool()).execute(
                    "UPDATE assessments.uploads SET status='processing' WHERE dataset_id=$1 AND job_id=$2",
                    UUID(payload["datasetId"]), job_id,
                )
                report, evidence = await build(
                    UUID(payload["datasetId"]), json.loads(job["subscription_ids"]), job["stale_days"],
                )
                snapshot = report_snapshots.build_snapshot(
                    report, json.loads(job["subscription_ids"]), job["stale_days"],
                )
                await report_snapshots.publish_snapshot(snapshot, update_pointers=False)
                await assessment_store.publish(
                    snapshot, job, report_snapshots.snapshot_blob_name(snapshot),
                    history=evidence.history, coverage=evidence.coverage,
                )
        finally:
            active_budget.reset(budget_token)
    except AssessmentLimitError as error:
        logger.warning("Assessment %s exceeded its capacity budget: %s", job_id, error)
        await assessment_store.fail(job_id, token, str(error), retryable=False, code="capacity_limit")
    except HTTPException as error:
        logger.exception("Assessment %s failed during collection", job_id)
        detail = error.detail if isinstance(error.detail, str) else error.detail.get("message", "Collection failed.")
        await assessment_store.fail(
            job_id, token, detail, retryable=error.status_code >= 500, code="collection_failed",
        )
    except (FocusCostDataError, ValueError, KeyError) as error:
        logger.warning("Assessment %s rejected uploaded evidence: %s", job_id, error)
        await assessment_store.fail(job_id, token, str(error), retryable=False, code="invalid_focus_report")
    finally:
        await assessment_store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(UUID(sys.argv[1]), UUID(sys.argv[2])))
