import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services import resource_uptime

RESOURCE = "/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
DAY = datetime.now(timezone.utc).date() - timedelta(days=1)


def payload(values):
    start = datetime.combine(DAY, datetime.min.time(), tzinfo=timezone.utc)
    return {"value": [{"name": {"value": "VmAvailabilityMetric"}, "timeseries": [{"data": [
        {"timeStamp": (start + timedelta(minutes=index)).isoformat(), "average": value}
        for index, value in enumerate(values)
    ]}]}]}


def test_complete_availability_and_null_gaps_do_not_imply_uptime():
    complete = resource_uptime.parse_availability(RESOURCE, DAY, payload([1] * 720 + [0] * 720))
    assert complete.available_hours == 12
    assert complete.coverage_minutes == 1440
    partial = resource_uptime.parse_availability(RESOURCE, DAY, payload([1] * 720 + [None] * 720))
    assert partial.status == "partial"
    assert partial.available_hours is None
    assert partial.observed_available_hours == 12
    assert resource_uptime.parse_availability(RESOURCE, DAY, payload([None] * 1440)).status == "unavailable"


@pytest.mark.parametrize("value", [True, -1, 2, float("nan"), "1"])
def test_invalid_metric_values_cannot_establish_availability(value):
    assert resource_uptime.parse_availability(RESOURCE, DAY, payload([value] * 1440)).available_hours is None


def test_unavailable_telemetry_is_explicit_and_never_cached_as_success(monkeypatch):
    resource_uptime._cache.clear()

    async def denied(*args, **kwargs):
        request = httpx.Request("GET", "https://example.test/metrics")
        raise httpx.HTTPStatusError("denied", request=request, response=httpx.Response(403, request=request))

    monkeypatch.setattr(resource_uptime.arm_client, "_arm_request", denied)
    result = asyncio.run(resource_uptime.get_resource_availability(RESOURCE, DAY))
    assert result.status == "unavailable"
    assert "Access denied" in result.status_message
    assert not resource_uptime._cache
    assert asyncio.run(resource_uptime.get_resource_availability("/storage/account", DAY)).status == "unsupported"