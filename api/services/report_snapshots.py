"""Publish and load complete assessment snapshots from private Blob Storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings

from reports.models import FullReport, ReportSnapshot, ReportSnapshotSummary
from services import runtime_identity

REPORT_SCHEMA_VERSION = "1.0"
_GLOBAL_POINTER = "latest.json"


class ReportSnapshotNotFoundError(LookupError):
    pass


class ReportSnapshotIntegrityError(RuntimeError):
    pass


def normalize_subscription_ids(subscription_ids: list[str]) -> list[str]:
    normalized = sorted({value.strip().lower() for value in subscription_ids if value.strip()})
    if not normalized:
        raise ValueError("At least one subscription ID is required")
    return normalized


def scope_hash(subscription_ids: list[str], stale_days: int) -> str:
    scope = {
        "reportSchemaVersion": REPORT_SCHEMA_VERSION,
        "staleDays": stale_days,
        "subscriptionIds": normalize_subscription_ids(subscription_ids),
    }
    encoded = json.dumps(scope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


@lru_cache(maxsize=1)
def get_container_client() -> ContainerClient:
    credential = runtime_identity.credential()
    service = BlobServiceClient(os.environ["COST_EXPORT_STORAGE_URL"], credential=credential)
    return service.get_container_client(os.environ.get("REPORT_SNAPSHOT_CONTAINER", "report-snapshots"))


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _scope_pointer(scope: str) -> str:
    return f"scopes/{scope}/latest.json"


def _snapshot_blob(snapshot: ReportSnapshot) -> str:
    period = snapshot.report.report_metadata.period or "unknown-period"
    return f"scopes/{snapshot.scope_hash}/snapshots/{period}/{snapshot.snapshot_id}.json"


def _snapshot_manifest_blob(snapshot: ReportSnapshot) -> str:
    return f"{_snapshot_blob(snapshot)}.manifest.json"


def build_snapshot(
    report: FullReport,
    subscription_ids: list[str],
    stale_days: int,
    *,
    created_at: str | None = None,
) -> ReportSnapshot:
    if not report.completeness.complete:
        raise ValueError("Only complete reports can be published as snapshots")
    normalized = normalize_subscription_ids(subscription_ids)
    report_subscriptions = sorted(row.subscription_id.lower() for row in report.subscription_breakdown)
    if report_subscriptions != normalized:
        raise ValueError("Snapshot scope does not match the report subscription scope")
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    identity = {
        "createdAt": created_at,
        "period": report.report_metadata.period,
        "scopeHash": scope_hash(normalized, stale_days),
    }
    snapshot_id = hashlib.sha256(_json_bytes(identity)).hexdigest()[:24]
    return ReportSnapshot(
        snapshotId=snapshot_id,
        scopeHash=identity["scopeHash"],
        subscriptionIds=normalized,
        staleDays=stale_days,
        createdAt=created_at,
        reportSchemaVersion=REPORT_SCHEMA_VERSION,
        report=report,
    )


def _publish_snapshot(snapshot: ReportSnapshot) -> None:
    container = get_container_client()
    payload = _json_bytes(snapshot.model_dump(by_alias=True, mode="json"))
    blob_name = _snapshot_blob(snapshot)
    try:
        container.upload_blob(
            blob_name,
            payload,
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json"),
        )
    except ResourceExistsError:
        pass
    manifest_value = {
        "blobName": blob_name,
        "bytes": len(payload),
        "reportSchemaVersion": REPORT_SCHEMA_VERSION,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "snapshotId": snapshot.snapshot_id,
        "scopeHash": snapshot.scope_hash,
        "subscriptionIds": snapshot.subscription_ids,
        "staleDays": snapshot.stale_days,
        "createdAt": snapshot.created_at,
        "period": snapshot.report.report_metadata.period,
        "periodStart": snapshot.report.report_metadata.period_start,
        "periodEnd": snapshot.report.report_metadata.period_end,
        "currency": snapshot.report.report_metadata.currency,
        "costBasis": snapshot.report.report_metadata.cost_basis,
    }
    pointer = _json_bytes(manifest_value)
    settings = ContentSettings(content_type="application/json", cache_control="no-store")
    try:
        container.upload_blob(
            _snapshot_manifest_blob(snapshot),
            pointer,
            overwrite=False,
            content_settings=settings,
        )
    except ResourceExistsError:
        pass
    container.upload_blob(_scope_pointer(snapshot.scope_hash), pointer, overwrite=True, content_settings=settings)
    container.upload_blob(_GLOBAL_POINTER, pointer, overwrite=True, content_settings=settings)


async def publish_snapshot(snapshot: ReportSnapshot) -> None:
    await asyncio.to_thread(_publish_snapshot, snapshot)


def _download_bytes(container: ContainerClient, blob_name: str) -> bytes:
    return container.download_blob(blob_name).readall()


def _load_latest(subscription_ids: list[str] | None, stale_days: int) -> ReportSnapshot:
    container = get_container_client()
    expected_scope = scope_hash(subscription_ids, stale_days) if subscription_ids else None
    pointer_name = _scope_pointer(expected_scope) if expected_scope else _GLOBAL_POINTER
    try:
        pointer = json.loads(_download_bytes(container, pointer_name))
        payload = _download_bytes(container, str(pointer["blobName"]))
    except (ResourceNotFoundError, KeyError, json.JSONDecodeError) as error:
        raise ReportSnapshotNotFoundError(pointer_name) from error
    if int(pointer.get("bytes") or -1) != len(payload):
        raise ReportSnapshotIntegrityError("Report snapshot byte length does not match its pointer")
    if str(pointer.get("sha256") or "") != hashlib.sha256(payload).hexdigest():
        raise ReportSnapshotIntegrityError("Report snapshot digest does not match its pointer")
    try:
        snapshot = ReportSnapshot.model_validate_json(payload)
    except ValueError as error:
        raise ReportSnapshotIntegrityError("Report snapshot payload is invalid") from error
    if snapshot.report_schema_version != REPORT_SCHEMA_VERSION:
        raise ReportSnapshotIntegrityError("Report snapshot schema version is unsupported")
    if expected_scope and snapshot.scope_hash != expected_scope:
        raise ReportSnapshotIntegrityError("Report snapshot scope does not match the request")
    return snapshot


async def load_latest_snapshot(
    subscription_ids: list[str] | None = None,
    stale_days: int = 90,
) -> ReportSnapshot:
    return await asyncio.to_thread(_load_latest, subscription_ids, stale_days)


def _summary(manifest: dict) -> ReportSnapshotSummary:
    return ReportSnapshotSummary.model_validate(manifest)


def _list_snapshots(subscription_ids: list[str] | None, limit: int) -> list[ReportSnapshotSummary]:
    container = get_container_client()
    expected = set(normalize_subscription_ids(subscription_ids)) if subscription_ids else None
    summaries = []
    for blob in container.list_blobs(name_starts_with="scopes/"):
        name = str(getattr(blob, "name", ""))
        if not name.endswith(".json.manifest.json"):
            continue
        try:
            manifest = json.loads(_download_bytes(container, name))
            summary = _summary(manifest)
        except (ResourceNotFoundError, json.JSONDecodeError, ValueError, KeyError):
            continue
        if expected is not None and set(summary.subscription_ids) != expected:
            continue
        summaries.append(summary)
    unique = {item.snapshot_id: item for item in summaries}
    return sorted(unique.values(), key=lambda item: (item.created_at, item.snapshot_id), reverse=True)[:limit]


async def list_snapshots(
    subscription_ids: list[str] | None = None,
    *,
    limit: int = 50,
) -> list[ReportSnapshotSummary]:
    return await asyncio.to_thread(_list_snapshots, subscription_ids, limit)


def _load_snapshot(snapshot_id: str) -> ReportSnapshot:
    if not snapshot_id or len(snapshot_id) > 64 or not snapshot_id.replace("-", "").isalnum():
        raise ReportSnapshotNotFoundError(snapshot_id)
    container = get_container_client()
    matching_manifest = None
    for blob in container.list_blobs(name_starts_with="scopes/"):
        name = str(getattr(blob, "name", ""))
        if name.endswith(f"/{snapshot_id}.json.manifest.json"):
            matching_manifest = name
            break
    if not matching_manifest:
        raise ReportSnapshotNotFoundError(snapshot_id)
    try:
        manifest = json.loads(_download_bytes(container, matching_manifest))
        payload = _download_bytes(container, str(manifest["blobName"]))
    except (ResourceNotFoundError, json.JSONDecodeError, KeyError) as error:
        raise ReportSnapshotIntegrityError("Report snapshot manifest is invalid") from error
    if int(manifest.get("bytes") or -1) != len(payload):
        raise ReportSnapshotIntegrityError("Report snapshot byte length does not match its manifest")
    if str(manifest.get("sha256") or "") != hashlib.sha256(payload).hexdigest():
        raise ReportSnapshotIntegrityError("Report snapshot digest does not match its manifest")
    try:
        snapshot = ReportSnapshot.model_validate_json(payload)
    except ValueError as error:
        raise ReportSnapshotIntegrityError("Report snapshot payload is invalid") from error
    if snapshot.snapshot_id != snapshot_id:
        raise ReportSnapshotIntegrityError("Report snapshot ID does not match its manifest")
    return snapshot


async def load_snapshot(snapshot_id: str) -> ReportSnapshot:
    return await asyncio.to_thread(_load_snapshot, snapshot_id)