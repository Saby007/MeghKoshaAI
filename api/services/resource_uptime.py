"""Read-only VM platform availability with explicit minute-level coverage."""

import asyncio
import math
import re
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from services import arm_client

_VM_ID = re.compile(r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/?#]+/providers/Microsoft.Compute/virtualMachines/[^/?#]+$", re.IGNORECASE)
_limit = asyncio.Semaphore(4)
_cache: OrderedDict[tuple[str, str], tuple[float, "ResourceAvailability"]] = OrderedDict()


class ResourceAvailability(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    resource_id: str = Field(alias="resourceId")
    billing_date: str = Field(alias="date")
    status: Literal["complete", "partial", "unavailable", "unsupported"]
    status_message: str = Field(alias="statusMessage")
    available_hours: float | None = Field(default=None, alias="availableHours")
    observed_available_hours: float | None = Field(default=None, alias="observedAvailableHours")
    coverage_minutes: int = Field(default=0, alias="coverageMinutes")
    expected_minutes: int = Field(default=1440, alias="expectedMinutes")
    metric: str = "VmAvailabilityMetric"
    observed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), alias="observedAt")


def parse_availability(resource_id: str, day: date, payload: dict) -> ResourceAvailability:
    unavailable = ResourceAvailability(resourceId=resource_id, date=str(day), status="unavailable", statusMessage="Unambiguous minute-level availability evidence was not returned.")
    metrics = payload.get("value")
    if not isinstance(metrics, list):
        return unavailable
    matches = [item for item in metrics if isinstance(item, dict) and isinstance(item.get("name"), dict) and item["name"].get("value") == "VmAvailabilityMetric"]
    if len(matches) != 1 or matches[0].get("errorCode") not in (None, "Success"):
        return unavailable
    series = matches[0].get("timeseries")
    if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict):
        return unavailable
    samples = series[0].get("data")
    if not isinstance(samples, list):
        return unavailable
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    minutes: dict[int, float] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            return unavailable
        try:
            stamp = datetime.fromisoformat(sample["timeStamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError, AttributeError):
            return unavailable
        if stamp.tzinfo is None or stamp.second or stamp.microsecond:
            return unavailable
        if not start <= stamp < end:
            continue
        value = sample.get("average")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            return unavailable
        minute = int((stamp - start).total_seconds() / 60)
        if minute in minutes:
            return unavailable
        minutes[minute] = value
    if not minutes:
        return unavailable
    complete = len(minutes) == 1440
    hours = sum(minutes.values()) / 60
    return ResourceAvailability(
        resourceId=resource_id, date=str(day), status="complete" if complete else "partial",
        statusMessage="Observed VM platform availability, not application health or billable runtime. Missing samples remain unknown, including Azure-initiated stop/deallocate gaps.",
        availableHours=hours if complete else None, observedAvailableHours=hours, coverageMinutes=len(minutes),
    )


async def get_resource_availability(resource_id: str, day: date) -> ResourceAvailability:
    if not _VM_ID.fullmatch(resource_id):
        return ResourceAvailability(resourceId=resource_id, date=str(day), status="unsupported", statusMessage="A common uptime metric is not available for this resource type. No uptime is inferred from billing.")
    today = datetime.now(timezone.utc).date()
    if day >= today or day < today - timedelta(days=92):
        return ResourceAvailability(resourceId=resource_id, date=str(day), status="unavailable", statusMessage="Choose a complete UTC day inside the 93-day platform-metric retention window.")
    key = (resource_id.lower(), str(day))
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        _cache.move_to_end(key)
        return cached[1]
    query = urlencode({
        "api-version": "2023-10-01", "metricnames": "VmAvailabilityMetric",
        "metricnamespace": "Microsoft.Compute/virtualMachines", "aggregation": "Average", "interval": "PT1M",
        "timespan": f"{day}T00:00:00Z/{day + timedelta(days=1)}T00:00:00Z", "AutoAdjustTimegrain": "false",
    })
    try:
        async with asyncio.timeout(10), _limit:
            payload = await arm_client._arm_request("GET", f"{quote(resource_id, safe='/')}/providers/Microsoft.Insights/metrics?{query}", timeout=8)
        result = parse_availability(resource_id, day, payload)
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        reason = "Access denied" if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 403 else "Telemetry unavailable or throttled"
        return ResourceAvailability(resourceId=resource_id, date=str(day), status="unavailable", statusMessage=f"{reason}. Retry later; no uptime has been inferred.")
    if result.status == "complete":
        _cache[key] = (time.monotonic(), result)
        _cache.move_to_end(key)
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return result