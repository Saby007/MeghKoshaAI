import asyncio
from datetime import date, timedelta

import httpx
import pytest
from urllib.parse import parse_qs, urlsplit

from services import arm_client


def test_role_assignments_use_scoped_group_filter_and_read_all_pages(monkeypatch):
    calls = []
    collection = "/subscriptions/sub-1/providers/Microsoft.Authorization/roleAssignments"

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        calls.append(path)
        assert method == "GET"
        if len(calls) == 1:
            query = parse_qs(urlsplit(path).query)
            assert query["$filter"] == ["atScope() and assignedTo('user-1')"]
            assert query["api-version"] == ["2022-04-01"]
            return {"value": [{"id": "first"}], "nextLink": f"{arm_client.ARM_BASE}{collection}?page=2"}
        return {"value": [{"id": "group-grant", "properties": {"principalType": "Group"}}]}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    results = asyncio.run(arm_client.list_role_assignments_for_principal("sub-1", "user-1"))
    assert [item["id"] for item in results] == ["first", "group-grant"]
    assert len(calls) == 2
    assert calls[1] == f"{collection}?page=2"


def test_subscription_discovery_reads_all_pages_filters_and_deduplicates(monkeypatch):
    calls = []

    async def request(method, path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            return {"value": [{"subscriptionId": "sub-1", "state": "Enabled"}], "nextLink": f"{arm_client.ARM_BASE}/subscriptions?page=2"}
        return {"value": [
            {"subscriptionId": "sub-1", "state": "Enabled"},
            {"subscriptionId": "sub-2", "state": "Enabled"},
            {"subscriptionId": "sub-3", "state": "Disabled"},
        ]}

    monkeypatch.setattr(arm_client, "_arm_request", request)
    result = asyncio.run(arm_client.list_subscriptions())
    assert [item["subscriptionId"] for item in result] == ["sub-1", "sub-2"]
    assert calls == ["/subscriptions?api-version=2022-12-01", "/subscriptions?page=2"]


@pytest.mark.parametrize("response", [{}, {"value": None}, {"value": [None]}, {"value": [{"state": "Enabled"}]}])
def test_subscription_discovery_does_not_turn_bad_data_into_empty_success(monkeypatch, response):
    async def request(*args, **kwargs):
        return response

    monkeypatch.setattr(arm_client, "_arm_request", request)
    with pytest.raises(ValueError):
        asyncio.run(arm_client.list_subscriptions())


def test_subscription_discovery_late_page_failure_never_returns_partial_access(monkeypatch):
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"value": [{"subscriptionId": "sub-1", "state": "Enabled"}], "nextLink": "/subscriptions?page=2"}
        raise httpx.ReadTimeout("Next page unavailable")

    monkeypatch.setattr(arm_client, "_arm_request", request)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(arm_client.list_subscriptions())
    assert calls == 2


@pytest.mark.parametrize("next_link", ["https://example.test/subscriptions?page=2", "/different-collection?page=2", "/subscriptions?api-version=2022-12-01"])
def test_subscription_pagination_rejects_untrusted_paths_and_cycles(monkeypatch, next_link):
    async def request(*args, **kwargs):
        return {"value": [], "nextLink": next_link}

    monkeypatch.setattr(arm_client, "_arm_request", request)
    with pytest.raises(ValueError):
        asyncio.run(arm_client.list_subscriptions())


def test_execution_history_reads_all_pages_and_orders_by_submission_not_billing_period(monkeypatch):
    calls = []
    collection = "/subscriptions/sub-1/providers/Microsoft.CostManagement/exports/focus-closed-month-meghkoshaai/runHistory"

    async def request(method, path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            return {"value": [{"name": "old", "properties": {"submittedTime": "2026-09-01T00:00:00Z", "startDate": "2026-08-01"}}], "nextLink": f"{arm_client.ARM_BASE}{collection}?page=2"}
        return {"value": [{"name": "new-rerun", "properties": {"submittedTime": "2026-09-07T08:00:00+02:00", "startDate": "2026-07-01"}}]}

    monkeypatch.setattr(arm_client, "_arm_request", request)
    assert [item["name"] for item in asyncio.run(arm_client.list_cost_export_runs("sub-1"))] == ["new-rerun", "old"]
    assert len(calls) == 2


@pytest.mark.parametrize("timestamp", [None, "bad-time", "2026-09-07T08:00:00"])
def test_execution_history_with_unverifiable_order_is_unavailable(monkeypatch, timestamp):
    async def request(*args, **kwargs):
        return {"value": [{"properties": {"submittedTime": timestamp}}]}

    monkeypatch.setattr(arm_client, "_arm_request", request)
    with pytest.raises(ValueError):
        asyncio.run(arm_client.list_cost_export_runs("sub-1"))


@pytest.mark.parametrize("response", [{}, {"value": None}, {"value": {}}, {"value": ["invalid"]}])
def test_role_assignment_lookup_rejects_malformed_responses(monkeypatch, response):
    async def fake_arm_request(*args, **kwargs):
        return response

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    with pytest.raises(ValueError, match="Invalid role assignment response"):
        asyncio.run(arm_client.list_role_assignments_for_principal("sub-1", "user-1"))


@pytest.mark.parametrize("next_link", [
    "https://example.test/subscriptions/sub-1/providers/Microsoft.Authorization/roleAssignments?page=2",
    "https://management.azure.com/subscriptions/other/providers/Microsoft.Authorization/roleAssignments?page=2",
    "//example.test/subscriptions/sub-1/providers/Microsoft.Authorization/roleAssignments?page=2",
    "http://management.azure.com/subscriptions/sub-1/providers/Microsoft.Authorization/roleAssignments?page=2",
    42,
])
def test_role_assignment_lookup_rejects_untrusted_pagination(monkeypatch, next_link):
    calls = []

    async def fake_arm_request(method, path, **kwargs):
        calls.append(path)
        return {"value": [], "nextLink": next_link}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    with pytest.raises(ValueError):
        asyncio.run(arm_client.list_role_assignments_for_principal("sub-1", "user-1"))
    assert len(calls) == 1


def test_role_assignment_lookup_rejects_pagination_cycles(monkeypatch):
    async def fake_arm_request(method, path, **kwargs):
        return {"value": [], "nextLink": f"{arm_client.ARM_BASE}{path}"}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    with pytest.raises(ValueError, match="repeated a page"):
        asyncio.run(arm_client.list_role_assignments_for_principal("sub-1", "user-1"))


def test_cost_management_queries_are_serialized(monkeypatch):
    active_requests = 0
    max_active_requests = 0

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal active_requests, max_active_requests
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        await asyncio.sleep(0)
        active_requests -= 1
        return {"properties": {"columns": [], "rows": []}, "path": path}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    monkeypatch.setattr(arm_client, "_COST_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    arm_client._cost_cache.clear()

    async def run_queries():
        return await asyncio.gather(
            arm_client._cost_management_query("subscription-1", ["ResourceId"]),
            arm_client._cost_management_query("subscription-2", ["ResourceId"]),
        )

    results = asyncio.run(run_queries())

    assert max_active_requests == 1
    assert len(results) == 2


def test_cost_retry_delay_uses_longest_azure_header():
    request = httpx.Request("POST", "https://management.azure.com/cost")
    response = httpx.Response(
        429,
        request=request,
        headers={
            "Retry-After": "5",
            "x-ms-ratelimit-microsoft.consumption-retry-after": "30",
            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "12",
            "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "60",
        },
    )
    error = httpx.HTTPStatusError("throttled", request=request, response=response)

    assert arm_client._cost_retry_delay(error, attempt=0) == 60.0


def test_reservation_recommendations_paginate_cache_and_filter_term(monkeypatch):
    calls: list[str] = []

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        calls.append(path)
        if len(calls) == 1:
            return {
                "value": [
                    {"name": "one-year", "properties": {"term": "P1Y"}},
                    {"name": "three-year", "properties": {"term": "P3Y"}},
                ],
                "nextLink": "https://management.azure.com/next-page",
            }
        return {"value": [{"name": "one-year-2", "properties": {"term": "P1Y"}}]}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._recommendation_cache.clear()

    first = asyncio.run(
        arm_client.list_reservation_recommendations(
            "subscription-1", "Last30Days", "P1Y", "VirtualMachines"
        )
    )
    second = asyncio.run(
        arm_client.list_reservation_recommendations(
            "subscription-1", "Last30Days", "P1Y", "VirtualMachines"
        )
    )

    assert [item["name"] for item in first] == ["one-year", "one-year-2"]
    assert second == first
    assert calls == [
        "/subscriptions/subscription-1/providers/Microsoft.Consumption/reservationRecommendations?"
        "api-version=2024-08-01&%24filter=properties%2Fscope+eq+%27Single%27+and+"
        "properties%2FresourceType+eq+%27VirtualMachines%27+and+properties%2FlookBackPeriod+eq+%27Last30Days%27",
        "/next-page",
    ]


def test_savings_plan_recommendations_cache_valid_empty_response(monkeypatch):
    calls = 0

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._recommendation_cache.clear()

    first = asyncio.run(
        arm_client.list_savings_plan_recommendations("subscription-1", "Last7Days", "P3Y")
    )
    second = asyncio.run(
        arm_client.list_savings_plan_recommendations("subscription-1", "Last7Days", "P3Y")
    )

    assert first == []
    assert second == []
    assert calls == 1


def test_recommendations_retry_throttling(monkeypatch):
    calls = 0
    delays: list[float] = []

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("GET", "https://management.azure.com/recommendations")
            response = httpx.Response(
                429,
                request=request,
                headers={"x-ms-ratelimit-microsoft.consumption-retry-after": "0"},
            )
            raise httpx.HTTPStatusError("throttled", request=request, response=response)
        return {"value": []}

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    monkeypatch.setattr(arm_client.asyncio, "sleep", fake_sleep)
    arm_client._recommendation_cache.clear()

    result = asyncio.run(
        arm_client.list_savings_plan_recommendations("subscription-1", "Last60Days", "P1Y")
    )

    assert result == []
    assert calls == 2
    assert delays == [0.0]


def test_recommendation_scenario_rejects_unsupported_values():
    try:
        asyncio.run(
            arm_client.list_reservation_recommendations(
                "subscription-1", "Last90Days", "P1Y", "VirtualMachines"
            )
        )
    except ValueError as error:
        assert "lookback" in str(error)
    else:
        raise AssertionError("Unsupported lookback was accepted")


def test_resource_graph_queries_are_bounded(monkeypatch):
    active_requests = 0
    max_active_requests = 0

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal active_requests, max_active_requests
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        await asyncio.sleep(0.01)
        active_requests -= 1
        return {"data": []}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)

    async def run_queries():
        await asyncio.gather(
            *(arm_client.query_resource_graph(f"resources | take {index}", ["sub-1"]) for index in range(12))
        )

    asyncio.run(run_queries())

    assert max_active_requests <= 4


def test_resource_graph_retries_throttling(monkeypatch):
    calls = 0
    delays: list[float] = []

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://management.azure.com/resourcegraph")
            response = httpx.Response(429, request=request, headers={"Retry-After": "0"})
            raise httpx.HTTPStatusError("throttled", request=request, response=response)
        return {"data": [{"id": "resource-1"}]}

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    monkeypatch.setattr(arm_client.asyncio, "sleep", fake_sleep)

    result = asyncio.run(arm_client.query_resource_graph("resources", ["sub-1"]))

    assert result == [{"id": "resource-1"}]
    assert calls == 2
    assert delays == [0.0]


def _metric_response(values: dict[str, float | None]) -> dict:
    return {
        "value": [
            {
                "name": {"value": metric_name},
                "timeseries": [
                    {"data": [{} if value is None else {"total": value}]}
                ],
            }
            for metric_name, value in values.items()
        ]
    }


def test_zero_traffic_requires_every_metric_on_every_day(monkeypatch):
    calls: list[str] = []

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        calls.append(path)
        return _metric_response({"ByteCount": 0, "PacketCount": 0})

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._metric_cache.clear()

    result = asyncio.run(
        arm_client.assess_zero_traffic(
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat-1",
            "idle_nat_gateways",
            "2026-07-01",
            "2026-07-03",
        )
    )

    assert result["complete"] is True
    assert result["zeroTraffic"] is True
    assert result["daysChecked"] == 3
    assert len(calls) == 3
    assert all("interval=FULL" in path and "aggregation=Total" in path for path in calls)


def test_nonzero_traffic_is_not_idle(monkeypatch):
    responses = [
        _metric_response({"BitsInPerSecond": 0, "BitsOutPerSecond": 0}),
        _metric_response({"BitsInPerSecond": 0, "BitsOutPerSecond": 42}),
    ]

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        return responses.pop(0)

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._metric_cache.clear()

    result = asyncio.run(
        arm_client.assess_zero_traffic(
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/expressRouteCircuits/er-1",
            "idle_expressroute_circuits",
            "2026-07-01",
            "2026-07-02",
        )
    )

    assert result["complete"] is True
    assert result["zeroTraffic"] is False
    assert result["totalTraffic"] == 42


def test_missing_metric_is_incomplete_and_never_idle(monkeypatch):
    async def fake_arm_request(method, path, json=None, timeout=30.0):
        return _metric_response({"TunnelIngressBytes": 0, "TunnelEgressBytes": None})

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._metric_cache.clear()

    result = asyncio.run(
        arm_client.assess_zero_traffic(
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworkGateways/vpn-1",
            "idle_virtual_network_gateways",
            "2026-07-01",
            "2026-07-01",
        )
    )

    assert result["complete"] is False
    assert result["zeroTraffic"] is False
    assert result["daysChecked"] == 0


def test_metric_request_failure_is_incomplete_and_cached(monkeypatch):
    calls = 0

    async def fake_arm_request(method, path, json=None, timeout=30.0):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._metric_cache.clear()
    args = (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat-1",
        "idle_nat_gateways",
        "2026-07-01",
        "2026-07-01",
    )

    first = asyncio.run(arm_client.assess_zero_traffic(*args))
    second = asyncio.run(arm_client.assess_zero_traffic(*args))

    assert first == second
    assert first["complete"] is False
    assert first["zeroTraffic"] is False
    assert calls == 1


def test_collect_zero_traffic_findings_excludes_active_and_incomplete_candidates(monkeypatch):
    assessments = {
        "zero": {"complete": True, "zeroTraffic": True, "daysChecked": 31, "metricNames": ["ByteCount"], "totalTraffic": 0},
        "active": {"complete": True, "zeroTraffic": False, "daysChecked": 31, "metricNames": ["ByteCount"], "totalTraffic": 10},
        "missing": {"complete": False, "zeroTraffic": False, "daysChecked": 3, "metricNames": ["ByteCount"], "totalTraffic": 0},
    }

    async def fake_assess(resource_id, category, period_start, period_end):
        return assessments[resource_id.rsplit("/", 1)[-1]]

    monkeypatch.setattr(arm_client, "assess_zero_traffic", fake_assess)
    candidates = [
        {"id": f"/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/natGateways/{name}", "name": name}
        for name in ("zero", "active", "missing")
    ]

    findings, coverage = asyncio.run(
        arm_client.collect_zero_traffic_findings(
            "idle_nat_gateways", candidates, "2026-07-01", "2026-07-31"
        )
    )

    assert [finding["name"] for finding in findings] == ["zero"]
    assert findings[0]["evidenceType"] == "metrics_verified_idle"
    assert findings[0]["confidence"] == 0.95
    assert coverage == {
        "category": "idle_nat_gateways",
        "candidateCount": 3,
        "completeCount": 2,
        "zeroTrafficCount": 1,
        "unavailableCount": 1,
        "periodStart": "2026-07-01",
        "periodEnd": "2026-07-31",
    }


def test_storage_account_metrics_include_tiers_reads_and_tracking(monkeypatch):
    async def fake_arm_request(method, path, json=None, timeout=30.0):
        if "metricnames=ContainerUsedSize" in path:
            return {
                "value": [{
                    "name": {"value": "ContainerUsedSize"},
                    "timeseries": [
                        {
                            "metadatavalues": [{"name": {"value": "Tier"}, "value": "Hot"}],
                            "data": [{"average": 1024.0}],
                        },
                        {
                            "metadatavalues": [{"name": {"value": "Tier"}, "value": "Cool"}],
                            "data": [{"average": 2048.0}],
                        },
                    ],
                }]
            }
        if "metricnames=Transactions" in path:
            return _metric_response({"Transactions": 3})
        return {"properties": {"lastAccessTimeTrackingPolicy": {"enable": True}}}

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    arm_client._metric_cache.clear()
    resource_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/store1"

    result = asyncio.run(
        arm_client.assess_storage_account(resource_id, "2026-07-01", "2026-07-31")
    )

    assert result == {
        "complete": True,
        "capacityBytes": 3072.0,
        "tierBytes": {"Hot": 1024.0, "Cool": 2048.0},
        "readTransactions": 3.0,
        "lastAccessTrackingEnabled": True,
    }


def test_ai_deployment_usage_uses_equal_complete_windows(monkeypatch):
    async def fake_arm_request(method, path, json=None, timeout=30.0):
        if "/deployments?" in path:
            return {
                "value": [{
                    "name": "prod-gpt4o",
                    "sku": {"name": "GlobalStandard", "capacity": 10},
                    "properties": {"model": {"name": "gpt-4o", "version": "2024-11-20"}},
                }]
            }
        values = []
        start = date(2026, 8, 3)
        for offset in range(14):
            current = start + timedelta(days=offset)
            multiplier = 2 if current >= date(2026, 8, 10) else 1
            values.append({"timeStamp": f"{current}T00:00:00Z", "total": 700 * multiplier})
        return {
            "value": [
                {"name": {"value": "ProcessedPromptTokens"}, "timeseries": [{"data": values}]},
                {"name": {"value": "GeneratedTokens"}, "timeseries": [{"data": [
                    {**point, "total": point["total"] / 2} for point in values
                ]}]},
                {"name": {"value": "TokenTransaction"}, "timeseries": [{"data": [
                    {**point, "total": point["total"] * 1.5} for point in values
                ]}]},
            ]
        }

    async def no_price(*args, **kwargs):
        return None

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    monkeypatch.setattr(arm_client, "_ai_token_price_per_unit", no_price)
    arm_client._ai_usage_cache.clear()
    result = asyncio.run(arm_client.collect_ai_deployment_usage(
        [{
            "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/openai1",
            "name": "openai1",
            "subscriptionId": "sub",
            "location": "eastus2",
            "accountKind": "OpenAI",
        }],
        period_end=date(2026, 8, 16),
    ))

    usage = result["deployments"][0]
    assert usage["deploymentName"] == "prod-gpt4o"
    assert usage["modelName"] == "gpt-4o"
    assert usage["inputTokensPerDay"] == 1400
    assert usage["outputTokensPerDay"] == 700
    assert usage["totalTokensPerDay"] == 2100
    assert usage["trendPercentage"] == 1.0
    assert usage["trendLabel"] == "increasing"
    assert usage["estimatedCostDay"] is None


# Fixture rows mirror real, live Azure Retail Prices API responses (verified 2026-09-15)
# for productName 'Azure OpenAI', including the non-standard variant meters (batch, cached
# input, realtime audio) that a naive substring match would wrongly conflate with the plain
# synchronous chat meter.
_GPT4O_1120_GLOBAL_ROWS = [
    {"skuName": "gpt 4o 1120 Inp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.0025, "type": "Consumption"},
    {"skuName": "gpt 4o 1120 Outp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.01, "type": "Consumption"},
    {"skuName": "gpt 4o 1120 Batch Inp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.00125, "type": "Consumption"},
    {"skuName": "gpt-4o-aud-1217 Outp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.08, "type": "Consumption"},
    {"skuName": "gpt 4o 1120 Inp regnl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.003, "type": "Consumption"},
]


def test_ai_token_price_matches_exact_global_meter_and_ignores_variant_meters(monkeypatch):
    async def fake_items(filter_expr):
        assert "1120" in filter_expr
        return _GPT4O_1120_GLOBAL_ROWS

    monkeypatch.setattr(arm_client, "_retail_price_items", fake_items)
    arm_client._retail_price_cache.clear()
    result = asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2"))
    assert result == {"input": 0.0025 / 1000, "output": 0.01 / 1000}


def test_ai_token_price_requires_region_match_for_regional_sku(monkeypatch):
    rows = [
        {"skuName": "gpt 4o 1120 Inp regnl", "armRegionName": "westus", "unitOfMeasure": "1K Tokens", "retailPrice": 0.003, "type": "Consumption"},
        {"skuName": "gpt 4o 1120 Outp regnl", "armRegionName": "westus", "unitOfMeasure": "1K Tokens", "retailPrice": 0.012, "type": "Consumption"},
        {"skuName": "gpt 4o 1120 Inp regnl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.0031, "type": "Consumption"},
        {"skuName": "gpt 4o 1120 Outp regnl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.0121, "type": "Consumption"},
    ]

    async def fake_items(filter_expr):
        return rows

    monkeypatch.setattr(arm_client, "_retail_price_items", fake_items)
    arm_client._retail_price_cache.clear()
    result = asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "Standard", "eastus2"))
    assert result == {"input": 0.0031 / 1000, "output": 0.0121 / 1000}


def test_ai_token_price_is_none_when_ambiguous_or_unit_unrecognized(monkeypatch):
    async def ambiguous(filter_expr):
        return [
            {"skuName": "gpt 4o 1120 Inp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.0025, "type": "Consumption"},
            {"skuName": "gpt 4o 1120 Inp glbl", "armRegionName": "eastus", "unitOfMeasure": "1K Tokens", "retailPrice": 0.0025, "type": "Consumption"},
            {"skuName": "gpt 4o 1120 Outp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.01, "type": "Consumption"},
        ]

    async def unrecognized_unit(filter_expr):
        return [
            {"skuName": "gpt 4o 1120 Inp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1 Hour", "retailPrice": 0.0025, "type": "Consumption"},
            {"skuName": "gpt 4o 1120 Outp glbl", "armRegionName": "eastus2", "unitOfMeasure": "1K Tokens", "retailPrice": 0.01, "type": "Consumption"},
        ]

    monkeypatch.setattr(arm_client, "_retail_price_items", ambiguous)
    arm_client._retail_price_cache.clear()
    assert asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2")) is None

    monkeypatch.setattr(arm_client, "_retail_price_items", unrecognized_unit)
    arm_client._retail_price_cache.clear()
    assert asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2")) is None


def test_ai_token_price_caches_result_and_does_not_requery(monkeypatch):
    calls = []

    async def fake_items(filter_expr):
        calls.append(filter_expr)
        return _GPT4O_1120_GLOBAL_ROWS

    monkeypatch.setattr(arm_client, "_retail_price_items", fake_items)
    arm_client._retail_price_cache.clear()
    first = asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2"))
    second = asyncio.run(arm_client._ai_token_price_per_unit("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2"))
    assert first == second and len(calls) == 1


def test_retail_price_items_rejects_a_next_page_link_leaving_the_expected_service(monkeypatch):
    actual_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "Items": [{"skuName": "gpt 4o 1120 Inp glbl"}],
            "NextPageLink": "https://attacker.example/api/retail/prices?$skiptoken=evil",
        })

    def client(**options):
        return actual_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(arm_client.httpx, "AsyncClient", client)
    with pytest.raises(ValueError, match="left the expected service"):
        asyncio.run(arm_client._retail_price_items("contains(productName, 'Azure OpenAI')"))
    assert len(requests) == 1


def test_ai_deployment_usage_computes_estimated_cost_from_retail_prices(monkeypatch):
    async def fake_arm_request(method, path, json=None, timeout=30.0):
        if "/deployments?" in path:
            return {
                "value": [{
                    "name": "prod-gpt4o",
                    "sku": {"name": "GlobalStandard", "capacity": 10},
                    "properties": {"model": {"name": "gpt-4o", "version": "2024-11-20"}},
                }]
            }
        values = [{"timeStamp": f"{date(2026, 8, 3) + timedelta(days=offset)}T00:00:00Z", "total": 700} for offset in range(14)]
        return {
            "value": [
                {"name": {"value": "ProcessedPromptTokens"}, "timeseries": [{"data": values}]},
                {"name": {"value": "GeneratedTokens"}, "timeseries": [{"data": values}]},
                {"name": {"value": "TokenTransaction"}, "timeseries": [{"data": [
                    {**point, "total": point["total"] * 2} for point in values
                ]}]},
            ]
        }

    async def fake_items(filter_expr):
        return _GPT4O_1120_GLOBAL_ROWS

    monkeypatch.setattr(arm_client, "_arm_request", fake_arm_request)
    monkeypatch.setattr(arm_client, "_retail_price_items", fake_items)
    arm_client._ai_usage_cache.clear()
    arm_client._retail_price_cache.clear()
    result = asyncio.run(arm_client.collect_ai_deployment_usage(
        [{
            "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/openai1",
            "name": "openai1",
            "subscriptionId": "sub",
            "location": "eastus2",
            "accountKind": "OpenAI",
        }],
        period_end=date(2026, 8, 16),
    ))

    usage = result["deployments"][0]
    assert usage["inputTokensPerDay"] == 700
    assert usage["outputTokensPerDay"] == 700
    assert usage["estimatedCostDay"] == round(700 * (0.0025 / 1000) + 700 * (0.01 / 1000), 4)