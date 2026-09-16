"""Explicit FOCUS export setup, ADLS schedules and bounded six-month execution."""

from __future__ import annotations

import asyncio
import calendar
import os
import re
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from azure.core.exceptions import AzureError, HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.identity import ManagedIdentityCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from services import focus_export_control, user_arm_client
from services.entra_tokens import configured_uuid

MAX_RECORD_BYTES = 128 * 1024
MAX_SCHEDULES = 5000
_JSON = ContentSettings(content_type="application/json", cache_control="no-store")


class MonthRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime
    end: AwareDatetime
    status: Literal["pending", "submitting", "queued", "running", "succeeded", "failed"] = "pending"
    attempts: int = Field(default=0, ge=0, le=3)
    submitted_at: AwareDatetime | None = None
    retry_after: AwareDatetime | None = None
    baseline_ids: list[str] = Field(default_factory=list, max_length=100)
    run_id: str | None = None


class RefreshCycle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    status: Literal["running", "succeeded", "failed"] = "running"
    months: list[MonthRun] = Field(min_length=6, max_length=6)
    error: str | None = None


class StoredSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    window_months: Literal[6] = 6
    tenant_id: str
    subscription_id: str
    display_name: str
    created_by: str
    updated_by: str
    updated_at: AwareDatetime
    state: Literal["active", "paused", "deleted"] = "active"
    schedule_start_at: AwareDatetime
    next_run_at: AwareDatetime
    cycle: RefreshCycle | None = None
    previous_runs: list[RefreshCycle] = Field(default_factory=list, max_length=24)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def completed_months(now: datetime) -> list[MonthRun]:
    boundary = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = []
    for _ in range(6):
        end = boundary - timedelta(seconds=1)
        start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result.append(MonthRun(start=start, end=end))
        boundary = start
    return list(reversed(result))


def next_monthly_run(start: datetime, now: datetime) -> datetime:
    start, now = start.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if start > now:
        return start
    year, month = now.year, now.month
    for _ in range(2):
        candidate = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
        if candidate > now:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise ValueError("Unable to calculate next monthly run")


def _configuration() -> tuple[str, str, str]:
    if os.environ.get("APP_SCHEDULER_ENABLED", "").lower() != "true":
        raise HTTPException(status_code=503, detail="The six-month FOCUS worker is not enabled. Contact the deployment administrator.")
    try:
        tenant = configured_uuid(os.environ.get("AZURE_TENANT_ID"))
        identity = configured_uuid(os.environ.get("AZURE_CLIENT_ID"))
        url = os.environ.get("COST_EXPORT_STORAGE_URL", "")
        parsed = urlsplit(url)
        account = focus_export_control._storage_resource_id().rsplit("/", 1)[-1]
        if parsed.scheme != "https" or parsed.netloc != f"{account}.blob.core.windows.net" or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid ADLS endpoint")
        return tenant, identity, url
    except (ValueError, RuntimeError, TypeError):
        raise HTTPException(status_code=503, detail="FOCUS scheduler identity and ADLS configuration are incomplete. Contact the deployment administrator.") from None


@contextmanager
def _container():
    _, identity, url = _configuration()
    with ManagedIdentityCredential(client_id=identity) as credential:
        with BlobServiceClient(url, credential=credential, retry_total=0, connection_timeout=5, read_timeout=10) as service:
            yield service.get_container_client(os.environ.get("CONTROL_STATE_CONTAINER", "control-state"))


def _blob_name(subscription_id: str) -> str:
    tenant, _, _ = _configuration()
    return f"schedules/{tenant}/{configured_uuid(subscription_id)}.json"


def _read(blob, subscription_id: str) -> StoredSchedule | None:
    try:
        raw = blob.download_blob(offset=0, length=MAX_RECORD_BYTES + 1).readall()
    except ResourceNotFoundError:
        return None
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Schedule metadata exceeds its size limit")
    record = StoredSchedule.model_validate_json(raw)
    if record.tenant_id != _configuration()[0] or record.subscription_id != subscription_id:
        raise ValueError("Stored schedule scope mismatch")
    if record.cycle:
        expected = completed_months(record.cycle.started_at)
        if [(item.start, item.end) for item in expected] != [(item.start, item.end) for item in record.cycle.months]:
            raise ValueError("Stored schedule month window mismatch")
    return record


def _write(blob, record: StoredSchedule, **kwargs):
    payload = record.model_dump_json().encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("Schedule metadata exceeds its size limit")
    blob.upload_blob(payload, content_settings=_JSON, **kwargs)


@asynccontextmanager
async def _locked(subscription_id: str):
    try:
        with _container() as container:
            blob = container.get_blob_client(_blob_name(subscription_id))
            lease = await asyncio.to_thread(blob.acquire_lease, lease_duration=60)
            try:
                async with asyncio.timeout(40):
                    record = await asyncio.to_thread(_read, blob, subscription_id)
                    if record is None:
                        raise HTTPException(status_code=404, detail="Schedule not found.")

                    async def save():
                        await asyncio.to_thread(_write, blob, record, overwrite=True, lease=lease)

                    yield record, save
            finally:
                await asyncio.to_thread(lease.release)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Schedule not found.") from None
    except ResourceExistsError:
        raise HTTPException(status_code=409, detail="This schedule is being updated. Refresh and retry.") from None
    except HttpResponseError as error:
        if error.status_code in (409, 412):
            raise HTTPException(status_code=409, detail="This schedule is being updated. Refresh and retry.") from None
        raise HTTPException(status_code=503, detail="ADLS schedule access is unavailable. Contact the deployment administrator.") from None
    except (AzureError, ValueError, TimeoutError):
        raise HTTPException(status_code=503, detail="The schedule could not be safely read or updated. Retry later.") from None


async def _arm_request(method: str, path: str, body: dict | None = None, *, headers: dict | None = None):
    _, identity, _ = _configuration()
    async with AsyncManagedIdentityCredential(client_id=identity) as credential:
        token = await credential.get_token("https://management.azure.com/.default")
        async with httpx.AsyncClient(base_url="https://management.azure.com", timeout=45 if method == "PUT" else 10, follow_redirects=False) as client:
            response = await client.request(method, path, headers={**(headers or {}), "Authorization": f"Bearer {token.token}"}, json=body)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError("Export control response is too large")
    return response.json() if response.content else {}


async def _native_request(method: str, subscription_id: str, suffix: str = "", body: dict | None = None):
    path = focus_export_control._export_path(subscription_id)
    if suffix == "permissions":
        path = path.split("?", 1)[0] + "/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
    elif suffix:
        path = path.replace("?", f"/{suffix}?", 1)
    return await _arm_request(method, path, body)


async def verify_export(subscription_id: str):
    _configuration()
    try:
        export = await _native_request("GET", subscription_id)
        properties = export["properties"]
        destination = properties["deliveryInfo"]["destination"]
        identity = export["identity"]
        if (properties["definition"]["type"] != "FocusCost"
                or properties["schedule"]["status"] != "Inactive"
                or properties.get("dataOverwriteBehavior") != "OverwritePreviousReport"
                or properties.get("partitionData") is not True
                or destination.get("resourceId", "").lower() != focus_export_control._storage_resource_id().lower()
                or destination.get("container") != focus_export_control._container_name()
                or destination.get("rootFolderPath", "").rstrip("/") != f"focus/{subscription_id}"
                or destination.get("sasToken") or destination.get("storageAccount")
                or identity.get("type") != "SystemAssigned"
                or configured_uuid(identity.get("tenantId")) != _configuration()[0]):
            raise ValueError("Export prerequisites mismatch")
        configured_uuid(identity.get("principalId"))
        permission_page = await _native_request("GET", subscription_id, "permissions")
        permissions = permission_page.get("value")
        if not isinstance(permissions, list) or permission_page.get("nextLink"):
            raise ValueError("Worker permissions are incomplete")
        allowed = set()
        required = {"microsoft.costmanagement/exports/read", "microsoft.costmanagement/exports/action", "microsoft.costmanagement/exports/run/action"}
        for permission in permissions:
            actions, excluded = permission.get("actions"), permission.get("notActions", [])
            if not isinstance(actions, list) or not isinstance(excluded, list) or any(not isinstance(value, str) for value in actions + excluded):
                raise ValueError("Invalid worker permissions")
            if permission.get("condition"):
                continue
            allowed.update(action for action in required if any(fnmatchcase(action, value.lower()) for value in actions)
                           and not any(fnmatchcase(action, value.lower()) for value in excluded))
        if "microsoft.costmanagement/exports/read" not in allowed or not allowed.intersection({
            "microsoft.costmanagement/exports/action", "microsoft.costmanagement/exports/run/action",
        }):
            raise ValueError("Worker lacks existing export execution permission")
    except (httpx.HTTPError, AzureError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(status_code=503, detail="An existing inactive FOCUS export with approved identity, ADLS destination, overwrite settings and worker execution permissions is required. Contact the deployment administrator; no access will be deployed.") from None


def _export_setup_details(subscription: dict) -> dict:
    if subscription.get("readAccess") is not True or subscription.get("costAccess") is not True:
        raise HTTPException(status_code=403, detail="Subscription access must be verified before configuring its export.")
    _configuration()
    subscription_id = configured_uuid(subscription["subscriptionId"])
    storage_id = focus_export_control._storage_resource_id()
    container = focus_export_control._container_name()
    export_name = os.environ.get("COST_EXPORT_NAME", focus_export_control.EXPORT_NAME)
    parts = storage_id.split("/")
    if (len(parts) != 9 or [parts[index].lower() for index in (1, 3, 5, 6, 7)] != [
            "subscriptions", "resourcegroups", "providers", "microsoft.storage", "storageaccounts"]
            or any(character in storage_id for character in "?#%")
            or not re.fullmatch(r"[a-z0-9]{3,24}", parts[-1])
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", container)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", export_name)):
        raise HTTPException(status_code=503, detail="The deployment's FOCUS export destination is invalid.")
    configured_uuid(parts[2])
    return {"subscriptionId": subscription_id, "exportName": export_name, "storageResourceId": storage_id,
            "container": container, "rootFolderPath": f"focus/{subscription_id}", "format": "Csv",
            "dataVersion": focus_export_control.DATA_VERSION, "windowMonths": 6, "nativeSchedule": "Inactive",
            "destinationRole": "Storage Blob Data Contributor",
            "destinationRoleScope": f"{storage_id}/blobServices/default/containers/{container}"}


def _setup_permissions(payload: dict, required: set[str]) -> bool:
    entries = payload.get("value")
    if not isinstance(entries, list) or payload.get("nextLink"):
        raise ValueError("Permission results are incomplete")
    granted = set()
    for entry in entries:
        actions, excluded = entry.get("actions"), entry.get("notActions", [])
        if not isinstance(actions, list) or not isinstance(excluded, list) or any(
            not isinstance(value, str) for value in actions + excluded
        ):
            raise ValueError("Invalid permission actions")
        if entry.get("condition") or entry.get("conditionVersion"):
            continue
        granted.update(action for action in required if any(fnmatchcase(action, value.lower()) for value in actions)
                       and not any(fnmatchcase(action, value.lower()) for value in excluded))
    return required.issubset(granted)


async def _verify_export_setup(details: dict) -> None:
    permissions_suffix = "/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
    permissions = await _arm_request("GET", f"/subscriptions/{details['subscriptionId']}{permissions_suffix}")
    if not _setup_permissions(permissions, {"microsoft.costmanagement/exports/write"}):
        raise HTTPException(status_code=403, detail="The runtime managed identity needs pre-granted FOCUS export write permission on this subscription. Configure subscription IAM manually.")
    storage_id = details["storageResourceId"]
    permissions = await _arm_request("GET", f"{storage_id}{permissions_suffix}")
    if not _setup_permissions(permissions, {"microsoft.storage/storageaccounts/read", "microsoft.storage/storageaccounts/write",
                                           "microsoft.authorization/permissions/read", "microsoft.authorization/roleassignments/write"}):
        raise HTTPException(status_code=403, detail="The runtime managed identity needs pre-granted storage setup and role-assignment permissions for this destination. No setup permission will be granted by the application.")
    storage = await _arm_request("GET", f"{storage_id}?api-version=2023-05-01")
    properties = storage["properties"]
    network = properties.get("networkAcls", {})
    if (properties.get("allowSharedKeyAccess") is not False
            or properties.get("allowBlobPublicAccess") is not False or properties.get("allowedCopyScope")
            or properties.get("publicNetworkAccess") not in {"Enabled", "Disabled"} or network.get("defaultAction") != "Deny"
            or "AzureServices" not in str(network.get("bypass", "")).split(",")):
        raise HTTPException(status_code=409, detail="FOCUS export delivery requires shared keys disabled and pre-approved trusted-services access on a restricted storage endpoint. Storage networking will not be changed by the application.")
    destination = await _arm_request("GET", f"{details['destinationRoleScope']}?api-version=2023-05-01")
    if destination["properties"].get("publicAccess") != "None":
        raise HTTPException(status_code=409, detail="The destination container must already exist with public access disabled.")


def _export_setup_failure(error: Exception) -> HTTPException:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        if response.status_code == 429:
            user_arm_client._check_response(response, _configuration()[0], operation="FOCUS export configuration")
        if response.status_code == 401:
            return HTTPException(status_code=503, detail="The runtime managed identity's recently granted role assignment may still be propagating through Azure AD. This retries automatically every few minutes; no action is needed unless it persists beyond about 15 minutes.")
        if response.status_code == 403:
            return HTTPException(status_code=403, detail="Azure denied the export setup operation. Verify the runtime managed identity's pre-granted subscription and destination permissions.")
        if response.status_code in (409, 412):
            return HTTPException(status_code=409, detail="The export changed during configuration. Refresh its status before trying again.")
    return HTTPException(status_code=503, detail="FOCUS export configuration could not be confirmed. Refresh its status before trying again; no automatic retry was performed.")


async def export_configuration(subscription: dict) -> dict:
    try:
        details = _export_setup_details(subscription)
        subscription_id = details["subscriptionId"]
        try:
            existing = await _native_request("GET", subscription_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
        else:
            properties = existing["properties"]
            if (properties.get("format") != details["format"] or properties.get("compressionMode") != "gzip"
                    or properties["definition"].get("dataSet", {}).get("configuration", {}).get("dataVersion") != details["dataVersion"]):
                raise HTTPException(status_code=409, detail="The existing export's format or dataset version differs from this deployment's configuration. It was not changed.")
            try:
                await verify_export(subscription_id)
            except HTTPException:
                raise HTTPException(status_code=409, detail="An existing export could not be verified against this deployment's configuration and execution permissions. It was not changed.") from None
            return {**details, "state": "configured", "canConfigure": False}
        await _verify_export_setup(details)
        return {**details, "state": "missing", "canConfigure": True}
    except (httpx.HTTPError, AzureError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise _export_setup_failure(error) from None


async def configure_export(subscription: dict, *, allow_destination_role_assignment: bool):
    if allow_destination_role_assignment is not True:
        raise HTTPException(status_code=400, detail="Confirm the export identity's destination-container role assignment before configuring the export.")
    details = await export_configuration(subscription)
    if details["state"] == "configured":
        return {**details, "created": False}
    subscription_id = details["subscriptionId"]
    body = {"identity": {"type": "SystemAssigned"}, "location": focus_export_control._location(),
            "properties": focus_export_control._new_properties(subscription_id, _utc(datetime.now(timezone.utc) + timedelta(days=1)), "Inactive")}
    try:
        await _arm_request("PUT", focus_export_control._export_path(subscription_id), body, headers={"If-None-Match": "*"})
        confirmed = await export_configuration(subscription)
        if confirmed["state"] != "configured":
            raise HTTPException(status_code=503, detail="The export creation outcome could not be confirmed. Refresh its status before trying again.")
    except (httpx.HTTPError, AzureError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise _export_setup_failure(error) from None
    return {**confirmed, "created": True}


def _cycle_view(subscription_id: str, cycle: RefreshCycle | None):
    if cycle is None:
        return None
    return {"runId": cycle.run_id, "subscriptionId": subscription_id,
            "period": f"{cycle.months[0].start:%Y-%m} to {cycle.months[-1].start:%Y-%m}",
            "status": cycle.status, "startedAt": _utc(cycle.started_at),
            "completedAt": _utc(cycle.completed_at) if cycle.completed_at else None,
            "durationSeconds": int((cycle.completed_at - cycle.started_at).total_seconds()) if cycle.completed_at else None,
            "completedMonths": sum(item.status == "succeeded" for item in cycle.months), "windowMonths": 6,
            "error": cycle.error}


def schedule_view(subscription: dict, record: StoredSchedule | None):
    if record and record.state == "deleted":
        record = None
    return {"subscriptionId": subscription["subscriptionId"], "displayName": subscription["displayName"],
            "readAccess": subscription.get("readAccess") is True, "costAccess": subscription.get("costAccess") is True,
            "accessCheckMode": subscription.get("accessCheckMode", "permissions"), "windowMonths": 6,
            "state": record.state if record else "not_scheduled", "recurrence": "Monthly",
            "scheduleStartAt": _utc(record.schedule_start_at) if record else None,
            "nextRunAt": _utc(record.next_run_at) if record and record.state == "active" else None,
            "latestRun": _cycle_view(subscription["subscriptionId"], record.cycle) if record else None,
            "availability": "available", "statusMessage": None}


def _unavailable_view(subscription: dict, availability: str, message: str):
    view = schedule_view(subscription, None)
    view.update(state="unknown", availability=availability, statusMessage=message)
    return view


async def load(subscription: dict):
    if subscription.get("readAccess") is not True or subscription.get("costAccess") is not True:
        return _unavailable_view(subscription, "access_unavailable", subscription.get("accessIssue") or
                                 "The runtime managed identity's subscription access could not be verified.")
    try:
        _configuration()
    except HTTPException as error:
        return _unavailable_view(subscription, "configuration_unavailable", error.detail)
    subscription_id = subscription["subscriptionId"]
    try:
        await verify_export(subscription_id)
    except HTTPException as error:
        # Export setup is never attempted from a background poll; the schedule tab's
        # explicit Export action is the only thing allowed to create or write it.
        return _unavailable_view(subscription, "export_unavailable", error.detail)
    except (AzureError, ValueError):
        return _unavailable_view(subscription, "export_unavailable", "FOCUS schedule storage could not be safely read. Retry later.")
    try:
        with _container() as container:
            record = await asyncio.to_thread(_read, container.get_blob_client(_blob_name(subscription_id)), subscription_id)
        if record is None:
            start = _utc(datetime.now(timezone.utc) + timedelta(minutes=6))
            return await create(subscription, "system:auto-provision", start)
        return schedule_view(subscription, record)
    except HTTPException as error:
        return _unavailable_view(subscription, "export_unavailable", error.detail)
    except (AzureError, ValueError):
        return _unavailable_view(subscription, "export_unavailable", "FOCUS schedule storage could not be safely read. Retry later.")


async def create(subscription: dict, actor: str, schedule_start: str):
    normalized = focus_export_control._validate_schedule_start(schedule_start)
    subscription_id = configured_uuid(subscription["subscriptionId"])
    await verify_export(subscription_id)
    now = datetime.now(timezone.utc)
    record = StoredSchedule(tenant_id=_configuration()[0], subscription_id=subscription_id,
                            display_name=subscription["displayName"], created_by=actor, updated_by=actor, updated_at=now,
                            schedule_start_at=normalized, next_run_at=normalized)
    try:
        with _container() as container:
            await asyncio.to_thread(_write, container.get_blob_client(_blob_name(subscription_id)), record, overwrite=False)
    except ResourceExistsError:
        async with _locked(subscription_id) as (stored, save):
            if stored.state != "deleted":
                raise HTTPException(status_code=409, detail="A schedule already exists for this subscription. Resume or edit it instead.") from None
            stored.state, stored.updated_by, stored.updated_at = "active", actor, now
            stored.display_name = subscription["displayName"]
            stored.schedule_start_at, stored.next_run_at = record.schedule_start_at, record.next_run_at
            await save()
            return schedule_view(subscription, stored)
    except AzureError:
        raise HTTPException(status_code=503, detail="The verified schedule could not be saved in ADLS. Contact the deployment administrator.") from None
    return schedule_view(subscription, record)


async def update(subscription: dict, actor: str, state: str, schedule_start: str | None = None):
    if state not in {"active", "paused", "deleted"}:
        raise ValueError("Invalid schedule state")
    start = focus_export_control._validate_schedule_start(schedule_start) if schedule_start else None
    if state == "active":
        await verify_export(subscription["subscriptionId"])
    async with _locked(subscription["subscriptionId"]) as (record, save):
        if record.state == "deleted":
            raise HTTPException(status_code=404, detail="Schedule not found.")
        if state == "deleted" and record.cycle and record.cycle.status == "running":
            raise HTTPException(status_code=409, detail="The current six-month refresh must finish before removing its schedule.")
        record.state, record.updated_by, record.updated_at = state, actor, datetime.now(timezone.utc)
        if start:
            record.schedule_start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
            record.next_run_at = record.schedule_start_at
        await save()
        return schedule_view(subscription, record)


async def history(subscription_id: str):
    async with _locked(subscription_id) as (record, _):
        return [_cycle_view(subscription_id, cycle) for cycle in ([record.cycle] if record.cycle else []) + list(reversed(record.previous_runs))]


async def advance(subscription_id: str, *, now: datetime | None = None, force: bool = False, allow_new_cycle: bool = True):
    now = now or datetime.now(timezone.utc)
    async with _locked(subscription_id) as (record, save):
        if record.state == "deleted":
            raise HTTPException(status_code=404, detail="Schedule not found.")
        cycle = record.cycle
        if cycle is None or cycle.status != "running":
            # allow_new_cycle=False (the periodic worker tick) may only continue a cycle a manual
            # Export/Run action already started; it can never start one on its own.
            if not force and (not allow_new_cycle or record.state != "active" or record.next_run_at > now):
                return {"subscriptionId": subscription_id, "status": "not_due"}
            await verify_export(subscription_id)
            if cycle:
                record.previous_runs = (record.previous_runs + [cycle])[-24:]
            cycle = record.cycle = RefreshCycle(started_at=now, months=completed_months(now))
            await save()
        await verify_export(subscription_id)
        month = next((item for item in cycle.months if item.status != "succeeded"), None)
        if month is None:
            cycle.status, cycle.completed_at = "succeeded", now
            record.next_run_at = next_monthly_run(record.schedule_start_at, now)
            await save()
            return {"subscriptionId": subscription_id, "status": "succeeded"}
        if month.retry_after and month.retry_after > now:
            return {"subscriptionId": subscription_id, "status": "running"}
        payload = await _native_request("GET", subscription_id, "runHistory")
        runs = payload.get("value")
        if not isinstance(runs, list) or len(runs) > 100 or payload.get("nextLink") or any(
            not isinstance(run, dict) or not isinstance(run.get("name"), str)
            or not isinstance(run.get("properties"), dict) for run in runs
        ):
            raise HTTPException(status_code=503, detail="Export execution history could not be safely reconciled.")
        if month.status in {"submitting", "queued", "running"}:
            matches = [run for run in runs if isinstance(run, dict) and run.get("name") not in month.baseline_ids
                       and str(run.get("properties", {}).get("startDate", ""))[:10] == month.start.date().isoformat()
                       and str(run.get("properties", {}).get("endDate", ""))[:10] == month.end.date().isoformat()]
            if matches:
                latest = max(matches, key=lambda run: str(run["properties"].get("submittedTime", "")))
                month.run_id = latest.get("name")
                status = latest["properties"].get("status")
                if status == "Completed":
                    month.status = "succeeded"
                    month.baseline_ids = []
                elif status in {"Failed", "Timeout", "DataNotAvailable", "NewDataNotAvailable"}:
                    if month.attempts < 3 and status in {"Failed", "Timeout"}:
                        month.status, month.retry_after = "pending", now + timedelta(minutes=15)
                    else:
                        month.status, cycle.status, cycle.error = "failed", "failed", "A monthly export did not complete; the six-month refresh is incomplete."
                elif status in {"Queued", "InProgress", "DataReady"}:
                    month.status = "queued" if status == "Queued" else "running"
                else:
                    raise HTTPException(status_code=503, detail="Azure returned an unknown export execution state.")
            elif month.submitted_at and now - month.submitted_at > timedelta(hours=24):
                cycle.status, cycle.error = "failed", "Export submission could not be reconciled. Review native execution history before retrying."
        else:
            if record.state == "paused" and not force:
                return {"subscriptionId": subscription_id, "status": "paused"}
            if any(run.get("properties", {}).get("status") in {"Queued", "InProgress"} for run in runs):
                return {"subscriptionId": subscription_id, "status": "running"}
            month.status, month.submitted_at = "submitting", now
            month.baseline_ids = [str(run.get("name")) for run in runs]
            month.attempts += 1
            await save()
            try:
                await _native_request("POST", subscription_id, "run", {"timePeriod": {"from": _utc(month.start), "to": _utc(month.end)}})
                month.status = "queued"
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 429 and month.attempts < 3:
                    delay = error.response.headers.get("retry-after", "900")
                    seconds = max(60, min(int(delay), 86400)) if delay.isascii() and delay.isdigit() and len(delay) <= 6 else 900
                    month.status, month.retry_after = "pending", now + timedelta(seconds=seconds)
                elif error.response.status_code < 500:
                    cycle.status, cycle.error = "failed", "Azure denied the export request. Verify the existing export and worker permissions outside this application."
            except httpx.HTTPError:
                pass
        if all(item.status == "succeeded" for item in cycle.months):
            cycle.status = "succeeded"
        if cycle.status != "running":
            cycle.completed_at = now
            for completed_month in cycle.months:
                completed_month.baseline_ids = []
            record.next_run_at = next_monthly_run(record.schedule_start_at, now)
        await save()
        return {"subscriptionId": subscription_id, "status": "queued" if month.status == "queued" else cycle.status}


def scheduled_subscription_ids() -> list[str]:
    tenant, _, _ = _configuration()
    with _container() as container:
        result = []
        prefix = f"schedules/{tenant}/"
        for blob in container.list_blobs(name_starts_with=prefix):
            if len(result) >= MAX_SCHEDULES:
                raise ValueError("Schedule count exceeds the configured processing limit")
            result.append(configured_uuid(blob.name.removeprefix(prefix).removesuffix(".json")))
        return result


async def active_scheduled_subscription_ids() -> list[str]:
    subscription_ids = await asyncio.to_thread(scheduled_subscription_ids)
    active: list[str] = []
    with _container() as container:
        for subscription_id in subscription_ids:
            record = await asyncio.to_thread(_read, container.get_blob_client(_blob_name(subscription_id)), subscription_id)
            if record is not None and record.state == "active":
                active.append(subscription_id)
    return active