import asyncio
import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from azure.core.exceptions import AzureError

from findings.models import SqlMetricEvidence, SqlWorkloadEvidence
from findings.sql_inventory import SQL_INVENTORY_TYPES, classify_sql_resource, is_system_sql_database
from services import arm_client


_DATABASE_METRICS = ("cpu_percent", "physical_data_read_percent", "log_write_percent", "sessions_percent", "workers_percent")
_PROFILES = {
    "single_database": _DATABASE_METRICS,
    "pooled_database": _DATABASE_METRICS,
    "elastic_pool": _DATABASE_METRICS,
    "managed_instance": ("avg_cpu_percent", "avg_workers_percent"),
    "sql_vm": ("Percentage CPU",),
}
_CACHE: dict[tuple[str, str, tuple[str, ...]], tuple[float, SqlWorkloadEvidence]] = {}
_REPORT_BUDGET_SECONDS = 12.0
_MAX_RESOURCES = 100


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone is required")
    return parsed.astimezone(timezone.utc)


def summarize_metrics(resource_id: str, names: tuple[str, ...], data: dict, now: datetime) -> SqlWorkloadEvidence:
    end = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30)
    expected = {start + timedelta(days=index) for index in range(30)}
    evidence = SqlWorkloadEvidence(
        resourceId=resource_id, status="partial", reason="Missing or invalid daily metric samples.",
        windowStart=start.isoformat(), windowEnd=end.isoformat(), collectedAt=now.isoformat(),
    )
    values = data.get("value", []) if isinstance(data, dict) else []
    if not isinstance(values, list):
        return evidence
    complete = True
    for name in names:
        matches = [value for value in values if isinstance(value, dict) and isinstance(value.get("name"), dict) and value["name"].get("value") == name]
        metric = SqlMetricEvidence(name=name)
        evidence.metrics.append(metric)
        if len(matches) != 1 or matches[0].get("unit") != "Percent" or matches[0].get("errorCode") not in (None, "Success"):
            complete = False
            continue
        series = matches[0].get("timeseries")
        if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict) or series[0].get("metadatavalues"):
            complete = False
            continue
        samples = series[0].get("data")
        if not isinstance(samples, list):
            complete = False
            continue
        observed: dict[datetime, tuple[float, float]] = {}
        invalid = False
        for sample in samples:
            try:
                if not isinstance(sample, dict):
                    raise ValueError("Invalid sample")
                timestamp = _utc(sample["timeStamp"])
                if timestamp not in expected or timestamp in observed:
                    raise ValueError("Unexpected or duplicate sample")
                average, maximum = sample["average"], sample["maximum"]
                if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (average, maximum)):
                    raise ValueError("Invalid metric value")
                if not 0 <= average <= maximum <= 100:
                    raise ValueError("Invalid percentage range")
                observed[timestamp] = (average, maximum)
            except (KeyError, TypeError, ValueError, AttributeError):
                invalid = True
        metric.observed_days = len(observed)
        if observed:
            metric.average = sum(sample[0] for sample in observed.values()) / len(observed)
            metric.maximum = max(sample[1] for sample in observed.values())
        complete = complete and not invalid and set(observed) == expected
    if complete and names:
        evidence.status = "complete"
        evidence.reason = "All requested metrics returned valid daily averages and maxima for 30 complete UTC days; sub-day sample continuity is not proven."
    return evidence


async def enrich_sql_inventory(rows_by_category: dict[str, list[dict]], subscription_ids: list[str]) -> dict[str, list[dict]]:
    now = datetime.now(timezone.utc)
    authorized = {value.casefold() for value in subscription_ids}
    output = dict(rows_by_category)
    requests: dict[tuple[str, tuple[str, ...]], list[dict]] = {}
    for category in SQL_INVENTORY_TYPES:
        output[category] = [dict(row) for row in rows_by_category.get(category, [])]
        for row in output[category]:
            if is_system_sql_database(category, row):
                continue
            context = classify_sql_resource(category, row)
            names = _PROFILES.get(context.deployment_model, ())
            resource_id = context.compute_resource_id if context.deployment_model == "sql_vm" else row.get("id")
            resource_id = str(resource_id or "").rstrip("/")
            match = re.fullmatch(r"/subscriptions/([^/]+)/resourceGroups/[^/]+/providers/[a-zA-Z.]+(?:/[^/?#\s]+){2,}", resource_id, re.IGNORECASE)
            evidence = summarize_metrics(resource_id, names, {}, now)
            if not names or not match or match.group(1).casefold() not in authorized or match.group(1).casefold() != str(row.get("subscriptionId") or "").casefold():
                evidence.status = "unsupported"
                evidence.reason = "Metric profile or authorized resource identity is unavailable."
                row["sqlWorkloadEvidence"] = evidence.model_dump(by_alias=True)
                continue
            key = (resource_id.casefold(), names)
            if key not in requests and len(requests) >= _MAX_RESOURCES:
                evidence.status = "not_collected"
                evidence.reason = "Per-report SQL resource collection limit reached."
                row["sqlWorkloadEvidence"] = evidence.model_dump(by_alias=True)
                continue
            requests.setdefault(key, []).append(row)
    limit = asyncio.Semaphore(4)

    async def collect(key: tuple[str, tuple[str, ...]], rows: list[dict]) -> None:
        resource_id, names = key
        evidence = summarize_metrics(resource_id, names, {}, now)
        cache_key = (resource_id, evidence.window_end, names)
        cached = _CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            evidence = cached[1].model_copy(deep=True)
        else:
            query = urlencode({
                "api-version": "2018-01-01", "metricnames": ",".join(names),
                "timespan": f"{evidence.window_start}/{evidence.window_end}", "interval": "P1D", "aggregation": "Average,Maximum",
                "AutoAdjustTimegrain": "false",
            })
            try:
                async with limit:
                    data = await arm_client._arm_request("GET", f"{resource_id}/providers/Microsoft.Insights/metrics?{query}", timeout=8.0)
                evidence = summarize_metrics(resource_id, names, data, now)
            except httpx.HTTPStatusError as error:
                evidence.status = "unavailable"
                evidence.reason = {403: "Metric access denied.", 429: "Metric collection throttled.", 400: "Requested metrics are unsupported for this configuration."}.get(error.response.status_code, "Metric service unavailable.")
            except (AzureError, httpx.HTTPError, ValueError, TypeError, AttributeError):
                evidence.status = "unavailable"
                evidence.reason = "Metric response was unavailable or invalid."
            if evidence.status == "complete":
                if len(_CACHE) >= 256:
                    _CACHE.pop(next(iter(_CACHE)))
                _CACHE[cache_key] = (time.monotonic(), evidence.model_copy(deep=True))
        for row in rows:
            row["sqlWorkloadEvidence"] = evidence.model_dump(by_alias=True)

    try:
        await asyncio.wait_for(asyncio.gather(*(collect(key, rows) for key, rows in requests.items())), timeout=_REPORT_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        pass
    for (resource_id, names), rows in requests.items():
        for row in rows:
            if "sqlWorkloadEvidence" not in row:
                evidence = summarize_metrics(resource_id, names, {}, now)
                evidence.status = "not_collected"
                evidence.reason = "SQL metric collection exceeded the per-report time budget."
                row["sqlWorkloadEvidence"] = evidence.model_dump(by_alias=True)
    return output