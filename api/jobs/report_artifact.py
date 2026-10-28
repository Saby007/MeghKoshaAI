"""Generate original report downloads in the isolated worker, using SQL snapshots."""

import asyncio
import json
import logging
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field, JsonValue

from services import assessment_store, report_snapshots
from reports import exports as report_exports
from reports.cost_details import build_cost_detail_export

logger = logging.getLogger(__name__)


class ArtifactTask(BaseModel):
    operation: Literal["report", "custom", "cost"]
    snapshot_ids: list[str] = Field(alias="snapshotIds", min_length=1, max_length=12)
    report_type: Literal["executive", "full", "chargeback", "compliance", "finops"] = Field(default="full", alias="reportType")
    subscription_id: str | None = Field(default=None, alias="subscriptionId")
    modules: list[str] = Field(default_factory=list, max_length=13)
    request: dict[str, JsonValue] = Field(default_factory=dict)


async def run(job: asyncpg.Record) -> None:
    payload = ArtifactTask.model_validate_json(job["task_payload"])
    snapshots = []
    for snapshot_id in payload.snapshot_ids:
        snapshots.append(await assessment_store.sql_snapshot(job["tenant_id"], snapshot_id, complete=True))
    ids = sorted({value.lower() for snapshot in snapshots for value in snapshot.subscription_ids})
    if ids != report_snapshots.normalize_subscription_ids(json.loads(job["subscription_ids"])):
        raise ValueError("Report task scope does not match its stored snapshots.")
    if payload.operation == "custom":
        if not payload.modules or set(payload.modules) - report_exports.CUSTOM_REPORT_MODULES:
            raise ValueError("Invalid custom report modules.")
        artifact = await asyncio.to_thread(report_exports.build_custom_xlsx, snapshots, payload.modules)
    elif payload.operation == "cost":
        from main import CostDetailExportRequest
        request = CostDetailExportRequest.model_validate(payload.request)
        if request.snapshot_id != snapshots[0].snapshot_id or len(snapshots) != 1:
            raise ValueError("Cost download must use exactly its authorized snapshot.")
        artifact = await asyncio.to_thread(
            build_cost_detail_export, snapshots[0], request.start_date, request.end_date, request.filters,
            request.previous_start, request.previous_end, request.selected_dates,
        )
    else:
        if len(snapshots) != 1:
            raise ValueError("A standard download must use exactly one snapshot.")
        from main import _build_report_artifact
        artifact = await _build_report_artifact(snapshots[0], payload.report_type, payload.subscription_id)
    await assessment_store.publish_artifact(job, artifact.file_name, artifact.media_type, artifact.content)
