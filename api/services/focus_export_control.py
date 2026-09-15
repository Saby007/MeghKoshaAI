"""Native Azure Cost Management FOCUS export lifecycle operations."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx

from services import arm_client

EXPORT_NAME = "focus-closed-month-meghkoshaai"
API_VERSION = "2025-03-01"
DATA_VERSION = "1.2-preview"


def normalize_subscription_id(value: str) -> str:
    return str(UUID(value.strip())).lower()


def _storage_resource_id() -> str:
    value = os.environ.get("COST_EXPORT_STORAGE_RESOURCE_ID", "").rstrip("/")
    if not value:
        raise RuntimeError("COST_EXPORT_STORAGE_RESOURCE_ID is not configured")
    return value


def _container_name() -> str:
    return os.environ.get("COST_EXPORT_CONTAINER", "cost-exports")


def _location() -> str:
    return os.environ.get("COST_EXPORT_LOCATION", "eastus2")


def _export_path(subscription_id: str) -> str:
    subscription_id = normalize_subscription_id(subscription_id)
    export_name = os.environ.get("COST_EXPORT_NAME", EXPORT_NAME)
    return (
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/"
        f"exports/{export_name}?api-version={API_VERSION}"
    )


def _next_monthly_run(schedule_start: str, now: datetime | None = None) -> str:
    start = datetime.fromisoformat(schedule_start.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    candidate = start
    while candidate <= now:
        year = candidate.year + (1 if candidate.month == 12 else 0)
        month = 1 if candidate.month == 12 else candidate.month + 1
        day = min(candidate.day, 28)
        candidate = candidate.replace(year=year, month=month, day=day)
    return candidate.isoformat().replace("+00:00", "Z")


def _validate_schedule_start(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("A valid UTC schedule start is required") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Schedule start must be at least five minutes in the future")
    return parsed.isoformat().replace("+00:00", "Z")


def _new_properties(subscription_id: str, schedule_start: str, status: str) -> dict:
    schedule_end = (
        datetime.fromisoformat(schedule_start.replace("Z", "+00:00")) + timedelta(days=3650)
    ).isoformat().replace("+00:00", "Z")
    return {
        "compressionMode": "gzip",
        "dataOverwriteBehavior": "OverwritePreviousReport",
        "definition": {
            "type": "FocusCost",
            "timeframe": "TheLastMonth",
            "dataSet": {
                "granularity": "Daily",
                "configuration": {"dataVersion": DATA_VERSION},
            },
        },
        "deliveryInfo": {
            "destination": {
                "type": "AzureBlob",
                "resourceId": _storage_resource_id(),
                "container": _container_name(),
                "rootFolderPath": f"focus/{normalize_subscription_id(subscription_id)}",
            }
        },
        "exportDescription": "Closed-period FOCUS cost export for MeghKoshaAI reporting.",
        "format": "Csv",
        "partitionData": True,
        "schedule": {
            "status": status,
            "recurrence": "Monthly",
            "recurrencePeriod": {"from": schedule_start, "to": schedule_end},
        },
    }


def _update_body(export: dict, *, status: str | None = None, schedule_start: str | None = None) -> dict:
    properties = dict(export.get("properties") or {})
    properties.pop("runHistory", None)
    schedule = dict(properties.get("schedule") or {})
    if status is not None:
        schedule["status"] = status
    if schedule_start is not None:
        schedule_start = _validate_schedule_start(schedule_start)
        schedule["recurrence"] = "Monthly"
        schedule["recurrencePeriod"] = {
            "from": schedule_start,
            "to": (
                datetime.fromisoformat(schedule_start.replace("Z", "+00:00")) + timedelta(days=3650)
            ).isoformat().replace("+00:00", "Z"),
        }
    properties["schedule"] = schedule
    return {
        "eTag": export.get("eTag"),
        "identity": {"type": "SystemAssigned"},
        "location": export.get("location") or _location(),
        "properties": properties,
    }


async def get_export(subscription_id: str) -> dict | None:
    try:
        return await arm_client._arm_request("GET", _export_path(subscription_id), timeout=60.0)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return None
        raise


async def create_export(subscription_id: str, schedule_start: str) -> dict:
    subscription_id = normalize_subscription_id(subscription_id)
    schedule_start = _validate_schedule_start(schedule_start)
    if await get_export(subscription_id) is not None:
        raise ValueError("FOCUS export already exists")
    body = {
        "identity": {"type": "SystemAssigned"},
        "location": _location(),
        "properties": _new_properties(subscription_id, schedule_start, "Inactive"),
    }
    await arm_client._arm_request("PUT", _export_path(subscription_id), json=body, timeout=90.0)
    return await update_export(subscription_id, status="Active")


async def update_export(
    subscription_id: str,
    *,
    status: str | None = None,
    schedule_start: str | None = None,
) -> dict:
    if status not in {None, "Active", "Inactive"}:
        raise ValueError("Export status must be Active or Inactive")
    export = await get_export(subscription_id)
    if export is None:
        raise LookupError("FOCUS export was not found")
    return await arm_client._arm_request(
        "PUT",
        _export_path(subscription_id),
        json=_update_body(export, status=status, schedule_start=schedule_start),
        timeout=90.0,
    )


async def delete_export(subscription_id: str) -> None:
    if await get_export(subscription_id) is None:
        raise LookupError("FOCUS export was not found")
    await arm_client._arm_request("DELETE", _export_path(subscription_id), timeout=90.0)


async def run_export(subscription_id: str) -> dict:
    if await get_export(subscription_id) is None:
        raise LookupError("FOCUS export was not found")
    await arm_client._arm_request(
        "POST",
        _export_path(subscription_id).replace(
            f"?api-version={API_VERSION}",
            f"/run?api-version={API_VERSION}",
        ),
        timeout=60.0,
    )
    return {"subscriptionId": normalize_subscription_id(subscription_id), "status": "queued"}


async def run_all_exports(subscription_ids: list[str]) -> list[dict]:
    async def run_one(subscription_id: str) -> dict:
        try:
            return await run_export(subscription_id)
        except Exception as error:
            return {
                "subscriptionId": normalize_subscription_id(subscription_id),
                "status": "failed",
                "error": str(error),
            }

    return await asyncio.gather(*(run_one(value) for value in subscription_ids))


def export_view(subscription_id: str, subscription_name: str, export: dict | None, latest_run: dict | None) -> dict:
    if export is None:
        return {
            "subscriptionId": normalize_subscription_id(subscription_id),
            "displayName": subscription_name,
            "state": "not_scheduled",
            "recurrence": "Monthly",
            "scheduleStartAt": None,
            "nextRunAt": None,
            "latestRun": latest_run,
        }
    properties = export.get("properties") if isinstance(export, dict) else None
    if not isinstance(properties, dict):
        raise ValueError("Invalid export properties")
    schedule = properties.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("Invalid export schedule")
    period = schedule.get("recurrencePeriod", {})
    if not isinstance(period, dict):
        raise ValueError("Invalid export recurrence period")
    start = str(period.get("from") or "")
    state = {"Active": "active", "Inactive": "paused"}.get(str(schedule.get("status") or ""), "unknown")
    next_run = None
    if state == "active":
        if not start or schedule.get("recurrence") != "Monthly":
            state = "unknown"
        else:
            try:
                next_run = _next_monthly_run(start)
            except (ValueError, TypeError):
                state = "unknown"
    return {
        "subscriptionId": normalize_subscription_id(subscription_id),
        "displayName": subscription_name,
        "state": state,
        "recurrence": str(schedule.get("recurrence") or "Monthly"),
        "scheduleStartAt": start or None,
        "nextRunAt": next_run,
        "latestRun": latest_run,
    }


def run_view(run: dict) -> dict:
    properties = run.get("properties") if isinstance(run, dict) else None
    if not isinstance(properties, dict):
        raise ValueError("Invalid execution properties")
    status = str(properties.get("status") or "")
    normalized_status = {
        "Completed": "succeeded",
        "Failed": "failed",
        "InProgress": "running",
        "Queued": "queued",
    }.get(status, "unknown")
    started_at = str(properties.get("processingStartTime") or properties.get("submittedTime") or "")
    completed_at = str(properties.get("processingEndTime") or "")
    if completed_at.startswith("0001-"):
        completed_at = ""
    duration_seconds = None
    if started_at and completed_at:
        try:
            duration_seconds = max(
                0,
                int(
                    (
                        datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                        - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    ).total_seconds()
                ),
            )
        except (ValueError, TypeError):
            duration_seconds = None
    error = properties.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return {
        "runId": str(run.get("name") or ""),
        "period": str(properties.get("startDate") or "")[:7],
        "status": normalized_status,
        "startedAt": started_at or None,
        "completedAt": completed_at or None,
        "durationSeconds": duration_seconds,
        "error": str(error) if error else None,
    }
