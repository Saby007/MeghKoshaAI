import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from services import sql_metrics


RESOURCE_ID = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Sql/servers/server/databases/app"


def metric_response(names, now, maximum=20):
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"value": [{"name": {"value": name}, "unit": "Percent", "timeseries": [{"data": [
        {"timeStamp": (end - timedelta(days=30-index)).isoformat(), "average": 10, "maximum": maximum}
        for index in range(30)
    ]}]} for name in names]}


def test_complete_window_preserves_maxima_and_coverage():
    now = datetime.now(timezone.utc)
    result = sql_metrics.summarize_metrics(RESOURCE_ID, ("cpu_percent",), metric_response(("cpu_percent",), now, 92), now)
    assert result.status == "complete"
    assert result.expected_days == 30
    assert result.metrics[0].maximum == 92
    assert result.metrics[0].average == 10
    assert result.metrics[0].observed_days == 30


@pytest.mark.parametrize("defect", ["missing_day", "duplicate", "null", "nan", "negative", "wrong_unit", "extra_dimension", "wrong_timestamp", "wrong_aggregation", "bad_name"])
def test_bad_metric_evidence_never_becomes_complete(defect):
    now = datetime.now(timezone.utc)
    data = metric_response(("cpu_percent",), now)
    metric = data["value"][0]
    samples = metric["timeseries"][0]["data"]
    if defect == "missing_day":
        samples.pop()
    elif defect == "duplicate":
        samples.append(samples[0])
    elif defect == "null":
        samples[0]["maximum"] = None
    elif defect == "nan":
        samples[0]["average"] = float("nan")
    elif defect == "negative":
        samples[0]["average"] = -1
    elif defect == "wrong_unit":
        metric["unit"] = "Count"
    elif defect == "extra_dimension":
        metric["timeseries"].append({"data": []})
    elif defect == "wrong_timestamp":
        samples[0]["timeStamp"] = now.isoformat()
    elif defect == "wrong_aggregation":
        samples[0].pop("maximum")
    else:
        metric["name"] = "cpu_percent"
    assert sql_metrics.summarize_metrics(RESOURCE_ID, ("cpu_percent",), data, now).status == "partial"


def _rows(subscription_id="sub-1"):
    return {"sql_databases_and_pools": [{"id": RESOURCE_ID, "type": "microsoft.sql/servers/databases", "elasticPoolId": "", "subscriptionId": subscription_id}]}


def test_collector_bounds_reads_and_caches_complete_results(monkeypatch):
    sql_metrics._CACHE.clear()
    calls = []

    async def request(method, path, **kwargs):
        calls.append(path)
        assert method == "GET"
        query = parse_qs(urlsplit(path).query)
        assert query["interval"] == ["P1D"]
        assert query["aggregation"] == ["Average,Maximum"]
        return metric_response(tuple(query["metricnames"][0].split(",")), datetime.now(timezone.utc))

    monkeypatch.setattr(sql_metrics.arm_client, "_arm_request", request)
    source = _rows()
    result = asyncio.run(sql_metrics.enrich_sql_inventory(source, ["sub-1"]))
    second = asyncio.run(sql_metrics.enrich_sql_inventory(source, ["sub-1"]))
    assert len(calls) == 1
    assert result == second
    assert "sqlWorkloadEvidence" not in source["sql_databases_and_pools"][0]
    assert result["sql_databases_and_pools"][0]["sqlWorkloadEvidence"]["status"] == "complete"
    sql_metrics._CACHE.clear()


@pytest.mark.parametrize("status, reason", [(403, "denied"), (429, "throttled"), (500, "unavailable")])
def test_collector_preserves_errors_and_does_not_cache_denial(monkeypatch, status, reason):
    sql_metrics._CACHE.clear()

    async def request(method, path, **kwargs):
        response = httpx.Response(status, request=httpx.Request(method, f"https://management.azure.com{path}"))
        response.raise_for_status()

    monkeypatch.setattr(sql_metrics.arm_client, "_arm_request", request)
    result = asyncio.run(sql_metrics.enrich_sql_inventory(_rows(), ["sub-1"]))
    evidence = result["sql_databases_and_pools"][0]["sqlWorkloadEvidence"]
    assert evidence["status"] == "unavailable"
    assert reason in evidence["reason"]
    assert sql_metrics._CACHE == {}


def test_unauthorized_or_mismatched_resource_is_not_fetched(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Out-of-scope resource fetched")

    monkeypatch.setattr(sql_metrics.arm_client, "_arm_request", unexpected)
    result = asyncio.run(sql_metrics.enrich_sql_inventory(_rows("sub-other"), ["sub-other"]))
    assert result["sql_databases_and_pools"][0]["sqlWorkloadEvidence"]["status"] == "unsupported"


def test_time_budget_does_not_fail_the_report_or_manufacture_zero(monkeypatch):
    sql_metrics._CACHE.clear()
    monkeypatch.setattr(sql_metrics, "_REPORT_BUDGET_SECONDS", 0.01)

    async def pending(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(sql_metrics.arm_client, "_arm_request", pending)
    result = asyncio.run(sql_metrics.enrich_sql_inventory(_rows(), ["sub-1"]))
    evidence = result["sql_databases_and_pools"][0]["sqlWorkloadEvidence"]
    assert evidence["status"] == "not_collected"
    assert all(metric["maximum"] is None for metric in evidence["metrics"])