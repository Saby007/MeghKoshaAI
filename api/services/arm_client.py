"""ARM, Cost Management and Resource Graph calls under the runtime managed identity."""

import asyncio
import logging
import math
import os
import re
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from services import runtime_identity

logger = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
_credential = None

# Cost Management's query API is far more aggressively rate-limited than plain ARM/
# Resource Graph calls, and re-running the assessment repeatedly (e.g. while testing)
# for the same subscription burns through that budget fast. Cache successful results
# per subscription+grouping for a few minutes so repeat requests don't re-hit Cost
# Management at all.
_cost_cache: dict[str, tuple[float, dict]] = {}
_COST_CACHE_TTL_SECONDS = 600.0
_cost_query_lock = asyncio.Lock()
_COST_MIN_REQUEST_INTERVAL_SECONDS = 15.0
_last_cost_request_at = 0.0

_TRAFFIC_METRIC_PROFILES = {
    "idle_virtual_network_gateways": ("TunnelIngressBytes", "TunnelEgressBytes"),
    "idle_nat_gateways": ("ByteCount", "PacketCount"),
    "idle_expressroute_circuits": ("BitsInPerSecond", "BitsOutPerSecond"),
}
_metric_cache: dict[tuple[str, str, str, str], tuple[float, dict]] = {}
_METRIC_CACHE_TTL_SECONDS = 900.0
_metric_query_semaphore = asyncio.Semaphore(4)
_ai_usage_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_AI_USAGE_CACHE_TTL_SECONDS = 900.0
_resource_graph_semaphore = asyncio.Semaphore(4)
_RESOURCE_GRAPH_MAX_RETRIES = 3
_recommendation_cache: dict[tuple[str, ...], tuple[float, list[dict]]] = {}
_RECOMMENDATION_CACHE_TTL_SECONDS = 3600.0
_recommendation_semaphore = asyncio.Semaphore(2)
_RECOMMENDATION_MAX_RETRIES = 3

RESERVATION_RESOURCE_TYPES = {
    "AppService",
    "AzureDataExplorer",
    "BlockBlob",
    "CosmosDB",
    "ManagedDisk",
    "MariaDB",
    "MySQL",
    "PostgreSQL",
    "RedHat",
    "RedisCache",
    "SQLDatabases",
    "SUSELinux",
    "SqlDataWarehouse",
    "VMwareCloudSimple",
    "VirtualMachines",
}
RECOMMENDATION_LOOKBACKS = {"Last7Days", "Last30Days", "Last60Days"}
RECOMMENDATION_TERMS = {"P1Y", "P3Y"}


async def _wait_for_cost_query_slot() -> None:
    global _last_cost_request_at

    delay = _COST_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_cost_request_at)
    if delay > 0:
        await asyncio.sleep(delay)
    _last_cost_request_at = time.monotonic()


def _cost_retry_delay(error: httpx.HTTPStatusError, attempt: int) -> float:
    retry_headers = (
        "Retry-After",
        "x-ms-ratelimit-microsoft.consumption-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    )
    delays: list[float] = []
    for header in retry_headers:
        try:
            delays.append(float(error.response.headers[header]))
        except (KeyError, ValueError):
            continue
    return max(delays, default=min(2**attempt, 30.0))


async def _token() -> str:
    global _credential
    if _credential is None:
        _credential = runtime_identity.async_credential()
    result = await _credential.get_token(f"{ARM_BASE}/.default")
    return result.token


async def _arm_request(method: str, path: str, json: dict | None = None, timeout: float = 30.0) -> dict:
    token = await _token()
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.request(
            method,
            f"{ARM_BASE}{path}",
            json=json,
            headers={"Authorization": f"Bearer {token}"},
        )
    res.raise_for_status()
    return res.json() if res.content else {}


async def _list_arm_collection(path: str, timeout: float = 30.0) -> list[dict]:
    collection_path = urlsplit(path).path
    rows: list[dict] = []
    visited: set[str] = set()
    while path:
        if path in visited:
            raise ValueError("ARM collection pagination repeated a page")
        visited.add(path)
        data = await _arm_request("GET", path, timeout=timeout)
        values = data.get("value")
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError("Invalid ARM collection response")
        rows.extend(values)
        next_link = data.get("nextLink")
        if not next_link:
            break
        if not isinstance(next_link, str):
            raise ValueError("Invalid ARM collection pagination link")
        parsed = urlsplit(next_link)
        if ((parsed.scheme, parsed.netloc) not in (("", ""), ("https", "management.azure.com"))
                or parsed.path.casefold() != collection_path.casefold() or parsed.fragment):
            raise ValueError("ARM pagination left the requested collection")
        path = urlunsplit(("", "", parsed.path, parsed.query, ""))
    return rows


async def list_subscriptions() -> list[dict]:
    rows = await _list_arm_collection("/subscriptions?api-version=2022-12-01")
    subscriptions: dict[str, dict] = {}
    for row in rows:
        if row.get("state") != "Enabled":
            continue
        subscription_id = row.get("subscriptionId")
        if not isinstance(subscription_id, str) or not subscription_id.strip():
            raise ValueError("Subscription response is missing an ID")
        subscriptions[subscription_id.casefold()] = row
    return list(subscriptions.values())


async def get_subscription(subscription_id: str) -> dict:
    return await _arm_request("GET", f"/subscriptions/{subscription_id}?api-version=2022-12-01")


async def list_role_assignments_for_principal(subscription_id: str, principal_object_id: str) -> list[dict]:
    """Read all at-scope assignments for a user, including ARM-resolved group grants.

    Callers must supply a verified Entra object ID and evaluate scope and role policy.
    This list alone is not an effective-permissions or deny-assignment evaluation.
    """
    query = urlencode({
        "api-version": "2022-04-01",
        "$filter": f"atScope() and assignedTo('{principal_object_id}')",
    })
    collection_path = f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleAssignments"
    path = f"{collection_path}?{query}"
    assignments: list[dict] = []
    visited: set[str] = set()
    while path:
        if path in visited:
            raise ValueError("Role assignment pagination repeated a page")
        visited.add(path)
        data = await _arm_request("GET", path)
        values = data.get("value")
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError("Invalid role assignment response")
        assignments.extend(values)
        next_link = data.get("nextLink")
        if not next_link:
            break
        if not isinstance(next_link, str):
            raise ValueError("Invalid role assignment pagination link")
        parsed = urlsplit(next_link)
        if (
            (parsed.scheme, parsed.netloc) not in (("", ""), ("https", "management.azure.com"))
            or parsed.path.casefold() != collection_path.casefold()
            or parsed.fragment
        ):
            raise ValueError("Role assignment pagination left the subscription collection")
        path = urlunsplit(("", "", parsed.path, parsed.query, ""))
    return assignments


async def list_resource_groups(subscription_id: str) -> list[dict]:
    """Resource groups (with their tags) for one subscription - used so cost-by-tag
    aggregation can fall back to a resource group's tag when the resource itself has
    none, i.e. tagging cascades down from the resource group to its resources."""
    groups: list[dict] = []
    path = f"/subscriptions/{subscription_id}/resourcegroups?api-version=2021-04-01"
    while path:
        data = await _arm_request("GET", path)
        groups.extend(data.get("value") or [])
        next_link = data.get("nextLink")
        path = next_link.removeprefix(ARM_BASE) if next_link else None
    return groups


async def list_native_budgets(subscription_id: str) -> list[dict]:
    """Azure Cost Management budgets configured directly on the subscription
    (Microsoft.Consumption/budgets) - the single source of truth for the Budgets tab."""
    return await _list_arm_collection(
        f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets?api-version=2023-11-01",
        timeout=10.0,
    )


async def get_native_budget(subscription_id: str, budget_name: str) -> dict | None:
    try:
        return await _arm_request(
            "GET",
            f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets/{budget_name}?api-version=2023-11-01",
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return None
        raise


async def put_native_budget(subscription_id: str, budget_name: str, properties: dict, e_tag: str | None) -> dict:
    body: dict = {"properties": properties}
    if e_tag:
        body["eTag"] = e_tag
    return await _arm_request(
        "PUT",
        f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets/{budget_name}?api-version=2023-11-01",
        json=body,
    )


async def delete_native_budget(subscription_id: str, budget_name: str) -> None:
    await _arm_request(
        "DELETE",
        f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets/{budget_name}?api-version=2023-11-01",
    )



async def query_resource_graph(query: str, subscription_ids: list[str]) -> list[dict]:
    """One Resource Graph query, across all selected subscriptions, with pagination."""
    rows: list[dict] = []
    skip_token: str | None = None
    while True:
        options: dict = {"$top": 1000}
        if skip_token:
            options["$skipToken"] = skip_token
        body = {"subscriptions": subscription_ids, "query": query, "options": options}
        for attempt in range(_RESOURCE_GRAPH_MAX_RETRIES + 1):
            try:
                async with _resource_graph_semaphore:
                    data = await _arm_request(
                        "POST",
                        "/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01",
                        json=body,
                    )
                break
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if attempt >= _RESOURCE_GRAPH_MAX_RETRIES or (status != 429 and status < 500):
                    raise
                try:
                    delay = float(error.response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(2**attempt, 8.0)
                await asyncio.sleep(max(delay, 0.0))
        rows.extend(data.get("data") or [])
        skip_token = data.get("$skipToken") or data.get("skipToken")
        if not skip_token:
            break
    return rows


def _metric_day_values(data: dict, expected_metrics: tuple[str, ...]) -> tuple[bool, float]:
    metrics: dict[str, list[float]] = {}
    for metric in data.get("value") or []:
        metric_name = str((metric.get("name") or {}).get("value") or "")
        values = metrics.setdefault(metric_name, [])
        for series in metric.get("timeseries") or []:
            for point in series.get("data") or []:
                total = point.get("total")
                if not isinstance(total, (int, float)) or isinstance(total, bool) or not math.isfinite(total) or total < 0:
                    return False, 0.0
                values.append(float(total))

    if any(not metrics.get(metric_name) for metric_name in expected_metrics):
        return False, 0.0
    return True, sum(sum(metrics[metric_name]) for metric_name in expected_metrics)


def _metric_series_total(data: dict, metric_name: str) -> tuple[float | None, dict[str, float]]:
    total = 0.0
    found = False
    by_tier: dict[str, float] = {}
    for metric in data.get("value") or []:
        if str((metric.get("name") or {}).get("value") or "") != metric_name:
            continue
        for series in metric.get("timeseries") or []:
            tier = ""
            for metadata in series.get("metadatavalues") or []:
                name = str((metadata.get("name") or {}).get("value") or "")
                if name.lower() in {"tier", "blobtier"}:
                    tier = str(metadata.get("value") or "")
            values = []
            for point in series.get("data") or []:
                value = point.get("average")
                if not isinstance(value, (int, float)):
                    value = point.get("total")
                if isinstance(value, (int, float)):
                    values.append(float(value))
            if not values:
                continue
            found = True
            series_value = values[-1] if metric_name == "ContainerUsedSize" else sum(values)
            total += series_value
            if tier:
                by_tier[tier] = by_tier.get(tier, 0.0) + series_value
    return (total if found else None), by_tier


async def assess_storage_account(
    resource_id: str,
    period_start: str,
    period_end: str,
) -> dict:
    cache_key = (resource_id.lower(), "storage_tiers", period_start, period_end)
    now = time.monotonic()
    cached = _metric_cache.get(cache_key)
    if cached and now - cached[0] < _METRIC_CACHE_TTL_SECONDS:
        return cached[1]

    end_exclusive = date.fromisoformat(period_end) + timedelta(days=1)
    timespan = (
        f"{period_start}T00:00:00Z/"
        f"{end_exclusive.isoformat()}T00:00:00Z"
    )
    blob_service_id = f"{resource_id.rstrip('/')}/blobServices/default"
    capacity_query = urlencode(
        {
            "api-version": "2018-01-01",
            "metricnames": "ContainerUsedSize",
            "timespan": timespan,
            "interval": "FULL",
            "aggregation": "Average",
            "$filter": "Tier eq '*'",
        }
    )
    reads_query = urlencode(
        {
            "api-version": "2018-01-01",
            "metricnames": "Transactions",
            "timespan": timespan,
            "interval": "FULL",
            "aggregation": "Total",
            "$filter": "ApiName eq 'GetBlob'",
        }
    )
    try:
        async with _metric_query_semaphore:
            capacity_data, reads_data, blob_properties = await asyncio.gather(
                _arm_request(
                    "GET",
                    f"{resource_id.rstrip('/')}/providers/microsoft.insights/metrics?{capacity_query}",
                    timeout=30.0,
                ),
                _arm_request(
                    "GET",
                    f"{blob_service_id}/providers/microsoft.insights/metrics?{reads_query}",
                    timeout=30.0,
                ),
                _arm_request("GET", f"{blob_service_id}?api-version=2023-05-01", timeout=30.0),
            )
        capacity, tier_bytes = _metric_series_total(capacity_data, "ContainerUsedSize")
        reads, _ = _metric_series_total(reads_data, "Transactions")
        tracking = ((blob_properties.get("properties") or {}).get("lastAccessTimeTrackingPolicy") or {})
        result = {
            "complete": capacity is not None and reads is not None,
            "capacityBytes": capacity,
            "tierBytes": tier_bytes,
            "readTransactions": reads,
            "lastAccessTrackingEnabled": tracking.get("enable"),
        }
    except Exception:
        logger.warning("Storage telemetry unavailable for %s", resource_id, exc_info=True)
        result = {
            "complete": False,
            "capacityBytes": None,
            "tierBytes": {},
            "readTransactions": None,
            "lastAccessTrackingEnabled": None,
        }
    _metric_cache[cache_key] = (time.monotonic(), result)
    return result


async def collect_storage_account_metrics(
    storage_accounts: list[dict],
    period_start: str,
    period_end: str,
) -> dict[str, dict]:
    assessments = await asyncio.gather(
        *(
            assess_storage_account(str(row.get("id") or ""), period_start, period_end)
            for row in storage_accounts
        )
    )
    return {
        str(row.get("id") or "").lower(): assessment
        for row, assessment in zip(storage_accounts, assessments)
        if row.get("id")
    }


def _metric_daily_totals(data: dict, names: tuple[str, ...]) -> dict[str, dict[str, float]]:
    totals = {name: {} for name in names}
    for metric in data.get("value") or []:
        metric_name = str((metric.get("name") or {}).get("value") or "")
        if metric_name not in totals:
            continue
        for series in metric.get("timeseries") or []:
            for point in series.get("data") or []:
                value = point.get("total")
                timestamp = str(point.get("timeStamp") or point.get("timestamp") or "")[:10]
                if timestamp and isinstance(value, (int, float)):
                    totals[metric_name][timestamp] = totals[metric_name].get(timestamp, 0.0) + float(value)
    return totals


async def _list_ai_deployments(account_id: str) -> list[dict]:
    data = await _arm_request("GET", f"{account_id.rstrip('/')}/deployments?api-version=2024-10-01")
    return data.get("value") or []


# Azure Retail Prices is public and unauthenticated - a separate, non-ARM lookup used only
# to estimate AI token cost. Meter naming is inconsistent and includes many non-standard
# variants (batch, cached-input, realtime audio, transcribe, fine-tuning) that must never be
# confused with the plain synchronous chat meter. Matching requires an EXACT token-set equality
# (not subset) against the plain <model><version><direction><deployment-type> shape; any extra
# qualifier token on either side breaks the match and the deployment is left unpriced rather
# than risk attributing the wrong meter's rate.
_RETAIL_PRICES_BASE = "https://prices.azure.com/api/retail/prices"
_retail_price_cache: dict[str, tuple[float, dict | None]] = {}
_RETAIL_PRICE_CACHE_TTL_SECONDS = 21600.0
_PRICE_TOKEN_SYNONYMS = {"inp": "input", "outp": "output", "glbl": "global", "regnl": "regional"}


def _price_tokens(value: str) -> frozenset[str]:
    raw = re.split(r"[^a-z0-9]+", value.lower())
    return frozenset(_PRICE_TOKEN_SYNONYMS.get(token, token) for token in raw if token)


def _price_unit_size(unit_of_measure: str) -> int | None:
    normalized = unit_of_measure.strip().lower().replace(",", "").replace(" ", "")
    if normalized == "1mtokens":
        return 1_000_000
    if normalized == "1ktokens":
        return 1_000
    return None


async def _retail_price_items(filter_expr: str) -> list[dict]:
    items: list[dict] = []
    url = f"{_RETAIL_PRICES_BASE}?{urlencode({'api-version': '2023-01-01-preview', '$filter': filter_expr})}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(20):
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("Items")
            if isinstance(page, list):
                items.extend(page)
            next_page = payload.get("NextPageLink")
            if not next_page or not isinstance(next_page, str):
                break
            parsed = urlsplit(next_page)
            if parsed.scheme != "https" or parsed.netloc != "prices.azure.com" or parsed.path != "/api/retail/prices":
                raise ValueError("Retail Prices pagination left the expected service")
            url = next_page
    return items


async def _ai_token_price_per_unit(model_name: str, model_version: str, sku_name: str, location: str) -> dict[str, float] | None:
    cache_key = f"{model_name}|{model_version}|{sku_name}|{location}".lower()
    now = time.monotonic()
    cached = _retail_price_cache.get(cache_key)
    if cached and now - cached[0] < _RETAIL_PRICE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        result = await _lookup_ai_token_price_per_unit(model_name, model_version, sku_name, location)
    except Exception:
        logger.warning("Azure Retail Prices lookup failed for %s %s", model_name, model_version, exc_info=True)
        result = None
    _retail_price_cache[cache_key] = (now, result)
    return result


async def _lookup_ai_token_price_per_unit(model_name: str, model_version: str, sku_name: str, location: str) -> dict[str, float] | None:
    base_tokens = _price_tokens(model_name)
    if not base_tokens:
        return None
    try:
        mmdd = datetime.strptime(model_version.strip(), "%Y-%m-%d").strftime("%m%d")
    except (ValueError, TypeError):
        return None
    sku_lower = sku_name.strip().lower()
    if sku_lower == "globalstandard":
        deployment_tokens, region_gated = frozenset({"global"}), False
    elif sku_lower == "datazonestandard":
        deployment_tokens, region_gated = frozenset({"data", "zone"}), False
    elif sku_lower == "standard":
        deployment_tokens, region_gated = frozenset({"regional"}), True
    else:
        return None
    region = location.strip().lower()
    items = await _retail_price_items(f"contains(productName, 'Azure OpenAI') and contains(skuName, '{mmdd}')")
    prices: dict[str, float] = {}
    for direction in ("input", "output"):
        expected = base_tokens | {mmdd, direction} | deployment_tokens
        matches = [
            item for item in items
            if item.get("type") == "Consumption"
            and isinstance(item.get("retailPrice"), (int, float)) and item["retailPrice"] > 0
            and _price_tokens(str(item.get("skuName") or "")) == expected
            and (not region_gated or str(item.get("armRegionName") or "").lower() == region)
        ]
        if len(matches) != 1:
            return None
        unit = _price_unit_size(str(matches[0].get("unitOfMeasure") or ""))
        if unit is None:
            return None
        prices[direction] = matches[0]["retailPrice"] / unit
    return prices if "input" in prices and "output" in prices else None


async def _collect_ai_deployment_metric(
    account: dict,
    deployment: dict,
    period_start: date,
    period_end: date,
) -> dict:
    account_id = str(account.get("id") or "")
    deployment_name = str(deployment.get("name") or "")
    properties = deployment.get("properties") or {}
    model = properties.get("model") or {}
    sku = deployment.get("sku") or {}
    base = {
        "accountId": account_id.lower(),
        "accountName": str(account.get("name") or account_id.rsplit("/", 1)[-1]),
        "subscriptionId": str(account.get("subscriptionId") or ""),
        "location": str(account.get("location") or "Unassigned"),
        "deploymentName": deployment_name,
        "modelName": str(model.get("name") or properties.get("modelName") or "Unknown"),
        "modelVersion": str(model.get("version") or properties.get("modelVersion") or ""),
        "skuName": str(sku.get("name") or ""),
        "capacity": float(sku["capacity"]) if isinstance(sku.get("capacity"), (int, float)) else None,
        "estimatedCostDay": None,
    }
    metric_profiles = (
        ("ProcessedPromptTokens", "GeneratedTokens", "TokenTransaction"),
        ("InputTokens", "OutputTokens", "TotalTokens"),
    )
    end_exclusive = period_end + timedelta(days=1)
    for metric_names in metric_profiles:
        query = urlencode(
            {
                "api-version": "2018-01-01",
                "metricnames": ",".join(metric_names),
                "timespan": f"{period_start}T00:00:00Z/{end_exclusive}T00:00:00Z",
                "interval": "P1D",
                "aggregation": "Total",
                "$filter": f"ModelDeploymentName eq '{deployment_name.replace(chr(39), chr(39) * 2)}'",
            }
        )
        try:
            async with _metric_query_semaphore:
                data = await _arm_request(
                    "GET",
                    f"{account_id.rstrip('/')}/providers/microsoft.insights/metrics?{query}",
                    timeout=30.0,
                )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 400:
                continue
            logger.warning("Azure OpenAI metrics unavailable for %s", deployment_name, exc_info=True)
            break
        except Exception:
            logger.warning("Azure OpenAI metrics unavailable for %s", deployment_name, exc_info=True)
            break
        daily = _metric_daily_totals(data, metric_names)
        if not daily[metric_names[0]] and not daily[metric_names[1]] and not daily[metric_names[2]]:
            continue
        current_dates = [str(period_end - timedelta(days=offset)) for offset in range(6, -1, -1)]
        previous_end = period_end - timedelta(days=7)
        previous_dates = [str(previous_end - timedelta(days=offset)) for offset in range(6, -1, -1)]
        current_input = sum(daily[metric_names[0]].get(day, 0.0) for day in current_dates)
        current_output = sum(daily[metric_names[1]].get(day, 0.0) for day in current_dates)
        current_total = sum(daily[metric_names[2]].get(day, 0.0) for day in current_dates)
        if current_total == 0 and current_input + current_output > 0:
            current_total = current_input + current_output
        previous_total = sum(daily[metric_names[2]].get(day, 0.0) for day in previous_dates)
        if previous_total == 0:
            previous_total = sum(
                daily[metric_names[0]].get(day, 0.0) + daily[metric_names[1]].get(day, 0.0)
                for day in previous_dates
            )
        if current_total == 0 and previous_total == 0:
            trend_percentage = 0.0
            trend_label = "stable"
        elif previous_total == 0:
            trend_percentage = None
            trend_label = "new usage"
        else:
            trend_percentage = (current_total - previous_total) / previous_total
            trend_label = "stable" if abs(trend_percentage) <= 0.05 else "increasing" if trend_percentage > 0 else "decreasing"
        input_per_day, output_per_day = current_input / 7, current_output / 7
        prices = await _ai_token_price_per_unit(base["modelName"], base["modelVersion"], base["skuName"], base["location"])
        estimated_cost_day = (
            round(input_per_day * prices["input"] + output_per_day * prices["output"], 4) if prices else None
        )
        return {
            **base,
            "inputTokensPerDay": round(input_per_day, 2),
            "outputTokensPerDay": round(output_per_day, 2),
            "totalTokensPerDay": round(current_total / 7, 2),
            "trendPercentage": trend_percentage,
            "trendLabel": trend_label,
            "evidenceStatus": "14-day UTC Azure Monitor token window",
            "estimatedCostDay": estimated_cost_day,
        }
    return {
        **base,
        "inputTokensPerDay": None,
        "outputTokensPerDay": None,
        "totalTokensPerDay": None,
        "trendPercentage": None,
        "trendLabel": "unavailable",
        "evidenceStatus": "Azure Monitor token metrics unavailable",
    }


async def collect_ai_deployment_usage(
    accounts: list[dict],
    *,
    period_end: date | None = None,
) -> dict:
    period_end = period_end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    period_start = period_end - timedelta(days=13)
    eligible = [
        account for account in accounts
        if str(account.get("accountKind") or "").lower() in {"openai", "aiservices"}
    ]
    deployments: list[tuple[dict, dict]] = []
    for account in eligible:
        account_id = str(account.get("id") or "")
        cache_key = (account_id.lower(), str(period_start), str(period_end))
        now = time.monotonic()
        cached = _ai_usage_cache.get(cache_key)
        try:
            if cached and now - cached[0] < _AI_USAGE_CACHE_TTL_SECONDS:
                items = cached[1]
            else:
                items = await _list_ai_deployments(account_id)
                _ai_usage_cache[cache_key] = (now, items)
        except Exception:
            logger.warning("Azure OpenAI deployments unavailable for %s", account_id, exc_info=True)
            continue
        deployments.extend((account, deployment) for deployment in items)
    usage = await asyncio.gather(
        *(
            _collect_ai_deployment_metric(account, deployment, period_start, period_end)
            for account, deployment in deployments
        )
    )
    return {
        "periodStart": str(period_start),
        "periodEnd": str(period_end),
        "deployments": usage,
        "eligibleAccountCount": len(eligible),
    }


async def assess_zero_traffic(
    resource_id: str,
    category: str,
    period_start: str,
    period_end: str,
) -> dict:
    """Require one aggregate point for every traffic metric on every UTC day.

    Missing metrics, missing days, and request failures are incomplete coverage and
    can never produce an idle finding.
    """
    metric_names = _TRAFFIC_METRIC_PROFILES.get(category)
    if metric_names is None:
        raise ValueError(f"No traffic metric profile for {category}")

    cache_key = (resource_id.lower(), category, period_start, period_end)
    now = time.monotonic()
    cached = _metric_cache.get(cache_key)
    if cached and now - cached[0] < _METRIC_CACHE_TTL_SECONDS:
        return cached[1]

    start_date = date.fromisoformat(period_start)
    end_date = date.fromisoformat(period_end)
    if end_date < start_date:
        raise ValueError("Metric period end must not precede start")

    expected_days = (end_date - start_date).days + 1
    total_traffic = 0.0
    current_date = start_date
    days_checked = 0
    while current_date <= end_date:
        next_date = current_date + timedelta(days=1)
        start_time = datetime.combine(current_date, datetime_time.min, timezone.utc)
        end_time = datetime.combine(next_date, datetime_time.min, timezone.utc)
        query = urlencode(
            {
                "api-version": "2018-01-01",
                "metricnames": ",".join(metric_names),
                "timespan": f"{start_time.isoformat().replace('+00:00', 'Z')}/{end_time.isoformat().replace('+00:00', 'Z')}",
                "interval": "FULL",
                "aggregation": "Total",
            }
        )
        path = f"{resource_id.rstrip('/')}/providers/microsoft.insights/metrics?{query}"
        try:
            async with _metric_query_semaphore:
                data = await _arm_request("GET", path, timeout=30.0)
        except Exception:
            logger.warning("Azure Monitor metrics unavailable for %s on %s", resource_id, current_date, exc_info=True)
            result = {
                "complete": False,
                "zeroTraffic": False,
                "daysChecked": days_checked,
                "expectedDays": expected_days,
                "totalTraffic": total_traffic,
                "metricNames": list(metric_names),
            }
            _metric_cache[cache_key] = (time.monotonic(), result)
            return result

        complete_day, day_total = _metric_day_values(data, metric_names)
        if not complete_day:
            result = {
                "complete": False,
                "zeroTraffic": False,
                "daysChecked": days_checked,
                "expectedDays": expected_days,
                "totalTraffic": total_traffic,
                "metricNames": list(metric_names),
            }
            _metric_cache[cache_key] = (time.monotonic(), result)
            return result

        total_traffic += day_total
        days_checked += 1
        current_date = next_date

    result = {
        "complete": days_checked == expected_days,
        "zeroTraffic": days_checked == expected_days and total_traffic == 0,
        "daysChecked": days_checked,
        "expectedDays": expected_days,
        "totalTraffic": total_traffic,
        "metricNames": list(metric_names),
    }
    _metric_cache[cache_key] = (time.monotonic(), result)
    return result


async def collect_zero_traffic_findings(
    category: str,
    candidates: list[dict],
    period_start: str,
    period_end: str,
) -> tuple[list[dict], dict]:
    assessments = await asyncio.gather(
        *(
            assess_zero_traffic(str(row.get("id") or ""), category, period_start, period_end)
            for row in candidates
        )
    )
    findings: list[dict] = []
    complete_count = 0
    unavailable_count = 0
    for row, assessment in zip(candidates, assessments):
        if not assessment["complete"]:
            unavailable_count += 1
            continue
        complete_count += 1
        if not assessment["zeroTraffic"]:
            continue
        findings.append(
            {
                **row,
                "evidenceType": "metrics_verified_idle",
                "confidence": 0.95,
                "metricDays": assessment["daysChecked"],
                "metricNames": assessment["metricNames"],
                "metricTotal": assessment["totalTraffic"],
                "metricPeriodStart": period_start,
                "metricPeriodEnd": period_end,
            }
        )

    coverage = {
        "category": category,
        "candidateCount": len(candidates),
        "completeCount": complete_count,
        "zeroTrafficCount": len(findings),
        "unavailableCount": unavailable_count,
        "periodStart": period_start,
        "periodEnd": period_end,
    }
    return findings, coverage


async def _cost_management_query(subscription_id: str, groupings: list[str]) -> dict:
    """POSTs a Cost Management query with a short-lived cache plus retry-with-backoff
    on 429/timeout - this API is rate limited (and honors a `Retry-After` header on
    429s) and noticeably slower than plain ARM/Resource Graph calls, unlike the rest
    of this module.
    """
    cache_key = f"{subscription_id}:{','.join(groupings)}"
    now = time.monotonic()
    cached = _cost_cache.get(cache_key)
    if cached and now - cached[0] < _COST_CACHE_TTL_SECONDS:
        return cached[1]

    # Azure applies Cost Management throttles across callers, not just per request.
    # Keep each retry sequence together so subscriptions do not retry in lockstep and
    # consume the same small request budget. Re-check the cache after acquiring the
    # lock because another assessment may have populated it while this one waited.
    async with _cost_query_lock:
        now = time.monotonic()
        cached = _cost_cache.get(cache_key)
        if cached and now - cached[0] < _COST_CACHE_TTL_SECONDS:
            return cached[1]

        definition = {
            "type": "ActualCost",
            "timeframe": "MonthToDate",
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": g} for g in groupings],
            },
        }
        path = f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                await _wait_for_cost_query_slot()
                data = await _arm_request("POST", path, json=definition, timeout=60.0)
                _cost_cache[cache_key] = (time.monotonic(), data)
                return data
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                is_rate_limited = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429
                if attempt < max_attempts - 1 and (is_rate_limited or isinstance(e, httpx.TransportError)):
                    delay = _cost_retry_delay(e, attempt) if is_rate_limited else min(2**attempt, 30.0)
                    logger.warning(
                        "Cost Management query retry %d/%d for %s after %s (waiting %.1fs)",
                        attempt + 1,
                        max_attempts - 1,
                        subscription_id,
                        type(e).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


async def get_cost_breakdown(subscription_id: str) -> tuple[dict[str, float], dict[str, float], float]:
    """One Cost Management query grouped by ResourceId AND ServiceFamily at once -
    replaces 3 separate queries (by-resource, by-service-family, plain total) that
    were hitting Cost Management's per-subscription rate limit (429s / ReadTimeouts,
    causing 502s at the ingress) when run for every selected subscription.
    Returns (cost_by_resource_id, cost_by_service_family, total_spend).
    """
    data = await _cost_management_query(subscription_id, ["ResourceId", "ServiceFamily"])
    columns = [c["name"] for c in data["properties"]["columns"]]
    cost_idx = columns.index("Cost")
    id_idx = columns.index("ResourceId")
    family_idx = columns.index("ServiceFamily")

    by_resource: dict[str, float] = {}
    by_family: dict[str, float] = {}
    total = 0.0
    for row in data["properties"]["rows"]:
        cost = float(row[cost_idx] or 0)
        resource_id = str(row[id_idx]).lower()
        family = str(row[family_idx] or "Other")
        by_resource[resource_id] = by_resource.get(resource_id, 0.0) + cost
        by_family[family] = by_family.get(family, 0.0) + cost
        total += cost
    return by_resource, by_family, total


async def list_advisor_recommendations(subscription_id: str) -> list[dict]:
    """Cost-category Azure Advisor recommendations for a subscription - used to
    reconcile Advisor's own claimed savings against what's actually billed (Advisor
    Reconciliation tab). Requires only Reader (already granted).
    """
    data = await _arm_request(
        "GET",
        f"/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01",
    )
    recs = data.get("value") or []
    return [r for r in recs if (r.get("properties") or {}).get("category") == "Cost"]


async def list_cost_export_runs(
    subscription_id: str,
    export_name: str = os.environ.get("COST_EXPORT_NAME", "focus-closed-month-meghkoshaai"),
) -> list[dict]:
    runs = await _list_arm_collection(
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/"
        f"exports/{export_name}/runHistory?api-version=2025-03-01",
        timeout=60.0,
    )
    def submitted_at(run: dict) -> datetime:
        properties = run.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("Execution history is missing properties")
        value = properties.get("submittedTime") or properties.get("processingStartTime")
        if not isinstance(value, str) or not value:
            raise ValueError("Execution history cannot be ordered without timestamps")
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Execution history timestamp is missing a timezone")
        return timestamp

    return sorted(runs, key=submitted_at, reverse=True)


def _validate_recommendation_scenario(
    look_back_period: str,
    term: str,
    resource_type: str | None = None,
) -> None:
    if look_back_period not in RECOMMENDATION_LOOKBACKS:
        raise ValueError(f"Unsupported recommendation lookback {look_back_period!r}")
    if term not in RECOMMENDATION_TERMS:
        raise ValueError(f"Unsupported recommendation term {term!r}")
    if resource_type is not None and resource_type not in RESERVATION_RESOURCE_TYPES:
        raise ValueError(f"Unsupported reservation resource type {resource_type!r}")


async def _list_recommendation_pages(cache_key: tuple[str, ...], path: str) -> list[dict]:
    now = time.monotonic()
    cached = _recommendation_cache.get(cache_key)
    if cached and now - cached[0] < _RECOMMENDATION_CACHE_TTL_SECONDS:
        return cached[1]

    async with _recommendation_semaphore:
        now = time.monotonic()
        cached = _recommendation_cache.get(cache_key)
        if cached and now - cached[0] < _RECOMMENDATION_CACHE_TTL_SECONDS:
            return cached[1]

        rows: list[dict] = []
        next_path: str | None = path
        while next_path:
            request_path = next_path.removeprefix(ARM_BASE)
            for attempt in range(_RECOMMENDATION_MAX_RETRIES + 1):
                try:
                    data = await _arm_request("GET", request_path, timeout=60.0)
                    break
                except (httpx.HTTPStatusError, httpx.TransportError) as error:
                    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 0
                    retryable = isinstance(error, httpx.TransportError) or status == 429 or status >= 500
                    if not retryable or attempt >= _RECOMMENDATION_MAX_RETRIES:
                        raise
                    delay = (
                        _cost_retry_delay(error, attempt)
                        if isinstance(error, httpx.HTTPStatusError)
                        else min(2**attempt, 30.0)
                    )
                    await asyncio.sleep(delay)
            rows.extend(data.get("value") or [])
            next_path = data.get("nextLink")

        _recommendation_cache[cache_key] = (time.monotonic(), rows)
        return rows


async def list_reservation_recommendations(
    subscription_id: str,
    look_back_period: str,
    term: str,
    resource_type: str,
) -> list[dict]:
    _validate_recommendation_scenario(look_back_period, term, resource_type)
    filter_expression = (
        "properties/scope eq 'Single' and "
        f"properties/resourceType eq '{resource_type}' and "
        f"properties/lookBackPeriod eq '{look_back_period}'"
    )
    query = urlencode({"api-version": "2024-08-01", "$filter": filter_expression})
    rows = await _list_recommendation_pages(
        ("reservation", subscription_id, look_back_period, resource_type),
        f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/"
        f"reservationRecommendations?{query}",
    )
    return [row for row in rows if str((row.get("properties") or {}).get("term") or "") == term]


async def list_savings_plan_recommendations(
    subscription_id: str,
    look_back_period: str,
    term: str,
) -> list[dict]:
    _validate_recommendation_scenario(look_back_period, term)
    filter_expression = (
        "properties/scope eq 'Single' and "
        f"properties/lookBackPeriod eq '{look_back_period}' and "
        f"properties/term eq '{term}'"
    )
    query = urlencode(
        {
            "api-version": "2026-06-01",
            "$filter": filter_expression,
            "$expand": "properties/allRecommendationDetails",
        }
    )
    return await _list_recommendation_pages(
        ("savings-plan", subscription_id, look_back_period, term),
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/"
        f"benefitRecommendations?{query}",
    )


async def get_untagged_resource_counts(subscription_ids: list[str]) -> dict[str, dict[str, int]]:
    """Per-subscription total resource count and untagged resource count, for the
    Governance & Risk tab's tagging maturity table.
    """
    query = "resources | project subscriptionId, tags"
    rows = await query_resource_graph(query, subscription_ids)
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        sub_id = str(row.get("subscriptionId", ""))
        bucket = counts.setdefault(sub_id, {"total": 0, "untagged": 0})
        bucket["total"] += 1
        tags = row.get("tags")
        if not tags:
            bucket["untagged"] += 1
    return counts
