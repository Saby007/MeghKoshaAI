"""Bounded immutable SQL projections; callers must authorize before every lookup."""

import hashlib
import json
from collections import OrderedDict

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from services.assessment_limits import AssessmentLimitError
from services.assessment_settings import settings

ProjectionKey = tuple[str, str, str, str]
MAX_CACHE_BYTES = 16 * 1024**2
MAX_CACHE_ENTRIES = 256
_entries: OrderedDict[ProjectionKey, bytes] = OrderedDict()
_bytes = 0
_headers = {"Cache-Control": "private, no-store"}


def key(tenant: str, snapshot_id: str, kind: str, query: BaseModel) -> ProjectionKey:
    encoded = json.dumps(query.model_dump(mode="json", by_alias=True), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return tenant, snapshot_id, kind, hashlib.sha256(encoded).hexdigest()


def get(cache_key: ProjectionKey) -> Response | None:
    payload = _entries.get(cache_key)
    if payload is None:
        return None
    _entries.move_to_end(cache_key)
    return Response(payload, media_type="application/json", headers=_headers)


def response(cache_key: ProjectionKey, value: dict) -> Response:
    global _bytes
    result = JSONResponse(value, headers=_headers)
    payload = bytes(result.body)
    if len(payload) > settings().response_bytes:
        raise AssessmentLimitError("SQL projection exceeds the configured response budget.")
    previous = _entries.pop(cache_key, None)
    if previous is not None:
        _bytes -= len(previous)
    if len(payload) <= MAX_CACHE_BYTES:
        _entries[cache_key] = payload
        _bytes += len(payload)
    while _bytes > MAX_CACHE_BYTES or len(_entries) > MAX_CACHE_ENTRIES:
        _, removed = _entries.popitem(last=False)
        _bytes -= len(removed)
    return result
