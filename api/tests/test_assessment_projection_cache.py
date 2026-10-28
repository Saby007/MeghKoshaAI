from types import SimpleNamespace

import pytest

from services import assessment_projection_cache as cache
from services.assessment_limits import AssessmentLimitError
from services.assessment_queries import CostQuery


@pytest.fixture(autouse=True)
def clear_cache():
    cache._entries.clear()
    cache._bytes = 0
    yield
    cache._entries.clear()
    cache._bytes = 0


def key(tenant="tenant", snapshot="one", **changes):
    return cache.key(tenant, snapshot, "costs", CostQuery(kind="metrics", **changes))


def test_immutable_projection_cache_is_tenant_snapshot_and_query_scoped():
    first = key(filters={"tagKey": "team", "tagValue": "one"})
    reordered = key(filters={"tagValue": "one", "tagKey": "team"})
    assert first == reordered
    assert key(tenant="another") != first and key(snapshot="another") != first
    value = cache.response(first, {"currentTotal": 125000})
    assert cache.get(first).body == value.body
    assert cache.get(key(tenant="another")) is None
    assert value.headers["cache-control"] == "private, no-store"


def test_cache_evicts_lru_entries_and_bounds_serialized_memory(monkeypatch):
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 100)
    monkeypatch.setattr(cache, "MAX_CACHE_ENTRIES", 2)
    cache.response(key(snapshot="one"), {"data": "a" * 30})
    cache.response(key(snapshot="two"), {"data": "b" * 30})
    assert cache.get(key(snapshot="one")) is not None
    cache.response(key(snapshot="three"), {"data": "c" * 30})
    assert cache.get(key(snapshot="two")) is None
    assert cache._bytes <= 100 and len(cache._entries) <= 2


def test_oversized_projection_fails_explicitly_without_caching(monkeypatch):
    monkeypatch.setattr(cache, "settings", lambda: SimpleNamespace(response_bytes=32))
    with pytest.raises(AssessmentLimitError, match="response budget"):
        cache.response(key(), {"data": "x" * 100})
    assert cache.get(key()) is None
