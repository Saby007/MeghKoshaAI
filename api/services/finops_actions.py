"""Versioned action state; the conditional latest write commits its immutable audit chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import ContentSettings

from reports.models import FinOpsActionState
from services.report_snapshots import get_container_client

_ACTION_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class ActionConflictError(RuntimeError):
    pass


class ActionIntegrityError(RuntimeError):
    pass


def _validate_action_id(action_id: str) -> str:
    if not _ACTION_ID.fullmatch(action_id):
        raise ValueError("Action ID is invalid")
    return action_id


def _latest_blob(scope_hash: str, action_id: str) -> str:
    return f"actions/{scope_hash}/latest/{_validate_action_id(action_id)}.json"


def _event_blob(state: FinOpsActionState, previous_event: str | None) -> str:
    event_id = hashlib.sha256(_bytes(state, previous_event)).hexdigest()
    return f"actions/{state.scope_hash}/events/{state.action_id}/{event_id}.json"


def _bytes(state: FinOpsActionState, previous_event: str | None, event_blob: str | None = None) -> bytes:
    payload = {**state.model_dump(by_alias=True, mode="json"), "previousEvent": previous_event}
    if event_blob is not None:
        payload["eventBlob"] = event_blob
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _decode(content: bytes, scope_hash: str, action_id: str) -> tuple[FinOpsActionState, dict]:
    try:
        payload = json.loads(content)
        state = FinOpsActionState.model_validate(payload)
        if state.scope_hash != scope_hash or state.action_id != action_id:
            raise ValueError("Action scope mismatch")
        return state, payload
    except (ValueError, TypeError) as error:
        raise ActionIntegrityError("Stored action state is invalid") from error


def _write_event(container, state: FinOpsActionState, previous_event: str | None) -> str:
    name = _event_blob(state, previous_event)
    payload = _bytes(state, previous_event)
    settings = ContentSettings(content_type="application/json", cache_control="no-store")
    try:
        container.upload_blob(name, payload, overwrite=False, content_settings=settings)
    except ResourceExistsError:
        if container.download_blob(name).readall() != payload:
            raise ActionIntegrityError("Existing action event does not match its content hash")
    return name


def _save(state: FinOpsActionState, expected_version: int) -> FinOpsActionState:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("Expected action version must be a nonnegative integer")
    container = get_container_client()
    name = _latest_blob(state.scope_hash, state.action_id)
    current = None
    previous_event = None
    etag = None
    try:
        download = container.download_blob(name)
        current, stored = _decode(download.readall(), state.scope_hash, state.action_id)
        etag = download.properties.etag
        if not etag:
            raise ActionIntegrityError("Action version metadata is unavailable")
        previous_event = stored.get("eventBlob")
        if previous_event is not None and previous_event != _event_blob(current, stored.get("previousEvent")):
            raise ActionIntegrityError("Action audit reference is invalid")
        if current.version > 0 and previous_event is None:
            raise ActionIntegrityError("Action audit reference is missing")
    except ResourceNotFoundError:
        current = None
    if (current.version if current is not None else 0) != expected_version:
        raise ActionConflictError("This action changed. Reload saved actions before saving again.")
    if current is not None and previous_event is not None:
        try:
            previous_payload = container.download_blob(previous_event).readall()
        except ResourceNotFoundError as error:
            raise ActionIntegrityError("Committed action audit event is missing") from error
        if previous_payload != _bytes(current, stored.get("previousEvent")):
            raise ActionIntegrityError("Committed action audit event differs from saved state")
    if current is not None and previous_event is None:
        previous_event = _write_event(container, current, None)
    changes = {"version": expected_version + 1}
    if current is not None and current.status == "completed" and state.status == "completed":
        changes["completed_at"] = current.completed_at
    saved = state.model_copy(update=changes)
    event_blob = _write_event(container, saved, previous_event)
    payload = _bytes(saved, previous_event, event_blob)
    settings = ContentSettings(content_type="application/json", cache_control="no-store")
    try:
        if current is None:
            container.upload_blob(name, payload, overwrite=False, content_settings=settings)
        else:
            container.upload_blob(name, payload, overwrite=True, content_settings=settings,
                                  etag=etag, match_condition=MatchConditions.IfNotModified)
    except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError) as error:
        raise ActionConflictError("This action changed. Reload saved actions before saving again.") from error
    return saved


async def save_action(state: FinOpsActionState, *, expected_version: int) -> FinOpsActionState:
    return await asyncio.to_thread(_save, state, expected_version)


def _list(scope_hash: str) -> list[FinOpsActionState]:
    container = get_container_client()
    values = []
    for blob in container.list_blobs(name_starts_with=f"actions/{scope_hash}/latest/"):
        try:
            content = container.download_blob(blob.name).readall()
            action_id = blob.name.rsplit("/", 1)[-1].removesuffix(".json")
            state, _ = _decode(content, scope_hash, action_id)
            values.append(state)
        except ResourceNotFoundError:
            continue
    return sorted(values, key=lambda item: item.action_id)


async def list_actions(scope_hash: str) -> list[FinOpsActionState]:
    return await asyncio.to_thread(_list, scope_hash)


def _history(scope_hash: str, action_id: str) -> list[FinOpsActionState]:
    container = get_container_client()
    try:
        current, stored = _decode(container.download_blob(_latest_blob(scope_hash, action_id)).readall(), scope_hash, action_id)
    except ResourceNotFoundError:
        return []
    event_blob = stored.get("eventBlob")
    if event_blob is None:
        if current.version:
            raise ActionIntegrityError("Action audit reference is missing")
        return [current]
    history = []
    expected_version = current.version
    visited = set()
    while event_blob:
        prefix = f"actions/{scope_hash}/events/{action_id}/"
        if not isinstance(event_blob, str) or not event_blob.startswith(prefix) or event_blob in visited or len(visited) >= 10000:
            raise ActionIntegrityError("Action audit chain is invalid")
        visited.add(event_blob)
        try:
            state, payload = _decode(container.download_blob(event_blob).readall(), scope_hash, action_id)
        except ResourceNotFoundError as error:
            raise ActionIntegrityError("Action audit event is missing") from error
        previous = payload.get("previousEvent")
        if state.version != expected_version or event_blob != _event_blob(state, previous):
            raise ActionIntegrityError("Action audit version or digest mismatch")
        if not history and state != current:
            raise ActionIntegrityError("Action audit head differs from committed state")
        history.append(state)
        expected_version -= 1
        event_blob = previous
    if history[-1].version not in {0, 1}:
        raise ActionIntegrityError("Action audit chain is incomplete")
    return history


async def list_action_history(scope_hash: str, action_id: str) -> list[FinOpsActionState]:
    return await asyncio.to_thread(_history, scope_hash, action_id)


def build_state(
    *,
    scope_hash: str,
    action_id: str,
    status: str,
    owner: str,
    due_date: str | None,
    realized_saving_month: float | None,
    note: str,
    actor: str,
    updated_at: str | None = None,
) -> FinOpsActionState:
    _validate_action_id(action_id)
    now = updated_at or datetime.now(timezone.utc).isoformat()
    return FinOpsActionState(
        scopeHash=scope_hash,
        actionId=action_id,
        status=status,
        owner=owner.strip(),
        dueDate=due_date or None,
        completedAt=now if status == "completed" else None,
        realizedSavingMonth=realized_saving_month,
        note=note.strip(),
        updatedAt=now,
        updatedBy=actor,
    )