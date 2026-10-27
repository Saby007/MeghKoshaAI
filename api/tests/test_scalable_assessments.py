import asyncio
import csv
import gzip
import io
import json
import calendar
from datetime import date, timedelta
import ssl
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

import main
from jobs import assessments
from reports.builder import build_full_report
from reports.models import CostHierarchyItem
from services import assessment_store, focus_cost_reader, report_snapshots
from services.assessment_limits import AssessmentBudget, AssessmentLimitError, active_budget
from services.assessment_settings import scalable_mode, settings

SUB = "11111111-1111-1111-1111-111111111111"
TENANT = SUB
BLOB = f"focus/{SUB}/20260701-20260731/run/data.csv.gz"


@pytest.fixture(autouse=True)
def clear_settings():
    settings.cache_clear()
    yield
    settings.cache_clear()


def write_focus(stream, rows=3, resources=2, period="2026-07"):
    start = date.fromisoformat(f"{period}-01")
    days = calendar.monthrange(start.year, start.month)[1]
    end = start + timedelta(days=days)
    columns = sorted(focus_cost_reader._REQUIRED_COLUMNS | {"Tags", "Region"})
    base = {column: "" for column in columns}
    base.update({
        "BillingPeriodStart": f"{start}T00:00:00Z", "BillingPeriodEnd": f"{end}T00:00:00Z",
        "SubAccountId": f"/subscriptions/{SUB}", "SubAccountName": "Synthetic subscription",
        "BillingCurrency": "USD", "PricingCurrency": "USD", "ChargeCategory": "Usage",
        "BilledCost": "0.01", "EffectiveCost": "0.01", "ContractedCost": "0.01", "ListCost": "0.01",
        "ListUnitPrice": "0.01", "ContractedUnitPrice": "0.01", "x_EffectiveUnitPrice": "0.01",
        "PricingQuantity": "1", "PricingUnit": "Hour", "PricingCategory": "Standard",
        "ResourceType": "Microsoft.Compute/virtualMachines", "ServiceCategory": "Compute",
        "ServiceName": "Virtual Machines", "SkuId": "sku", "SkuPriceId": "price",
        "x_ResourceGroupName": "rg", "Region": "eastus",
    })
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for index in range(rows):
        resource = index % resources
        writer.writerow({
            **base, "ChargePeriodStart": f"{period}-{(index // resources) % days + 1:02d}T00:00:00Z",
            "ResourceName": f"vm-{resource}",
            "ResourceId": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-{resource}",
            "Tags": '{"team":"synthetic","late":"observed"}' if index == rows - 1 else '{"team":"synthetic"}',
        })


def make_report(resources=0, stale_days=90):
    report = build_full_report(
        subscription_ids=[SUB], subscription_names={SUB: "Synthetic subscription"},
        per_sub_spend={SUB: float(resources or 100)}, resource_graph_rows={},
        cost_by_resource_id={}, service_family_spend={}, advisor_recommendations=[], untagged_counts={},
    )
    return report.model_copy(update={
        "report_metadata": report.report_metadata.model_copy(update={
            "period": "2026-07", "period_start": "2026-07-01", "period_end": "2026-07-31",
            "currency": "USD", "stale_days": stale_days,
        }),
        "completeness": report.completeness.model_copy(update={"complete": True, "status": "Reconciled"}),
        "executive_summary": report.executive_summary.model_copy(update={"active_resources": resources}),
        "cost_hierarchy": [CostHierarchyItem(
            subscriptionId=SUB, subscriptionName="Synthetic subscription", resourceGroup="rg",
            resourceId=f"/subscriptions/{SUB}/resources/vm-{index}", resourceName=f"vm-{index}",
            resourceType="Microsoft.Compute/virtualMachines", monthlySpend=1, pctOfTotal=1 / max(resources, 1),
        ) for index in range(resources)],
    })


def test_explicit_mode_and_invalid_limits_fail_closed(monkeypatch):
    monkeypatch.delenv("ASSESSMENT_MODE", raising=False)
    assert not scalable_mode()
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    assert scalable_mode()
    monkeypatch.setenv("ASSESSMENT_MODE", "automatic")
    with pytest.raises(ValueError, match="blob or postgres"):
        scalable_mode()
    monkeypatch.setenv("ASSESSMENT_RESPONSE_BYTES", "65536")
    with pytest.raises(ValueError, match="4096"):
        settings()


def test_openapi_distinguishes_queued_jobs_summaries_and_complete_reports():
    schema = main.app.openapi()
    accepted = schema["paths"]["/api/report"]["post"]["responses"]["202"]
    assert accepted["content"]["application/json"]["schema"]["$ref"].endswith("/BackgroundAssessmentJob")
    latest = schema["paths"]["/api/report/latest"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert {item["$ref"].rsplit("/", 1)[-1] for item in latest["anyOf"]} == {"ReportSnapshot", "ScalableReportSnapshot"}


def test_nested_collections_are_deferred_without_changing_complete_totals():
    original = {"executiveSummary": {"total": 123.45}, "categories": [
        {"name": str(index), "lines": [{"cost": 1, "name": f"vm-{row}"} for row in range(25)]}
        for index in range(25)
    ]}
    summary, collections = assessment_store.split_report(original)
    assert summary["executiveSummary"] == original["executiveSummary"]
    assert summary["categories"] == []
    assert len(dict(collections)["categories"]) == 25
    assert len(dict(collections)["categories.0.lines"]) == 25
    assert dict(collections)["categories"][0]["lines"] == []
    assert len(original["categories"][0]["lines"]) == 25
    assert len(assessment_store._json(summary).encode()) < 1024


def test_oversized_detail_record_is_rejected_not_truncated():
    with pytest.raises(AssessmentLimitError, match="detail-record"):
        list(assessment_store.record_rows(TENANT, "snapshot", "details", [{"name": "x" * 300000}]))


def test_streamed_gzip_and_spooled_tag_attribution_match_legacy_exactly(tmp_path):
    content = io.StringIO()
    write_focus(content)
    compressed = gzip.compress(content.getvalue().encode())
    path = tmp_path / "focus.csv.gz"
    path.write_bytes(compressed)
    expected = focus_cost_reader.build_focus_cost_data({BLOB: compressed}, [SUB])
    budget = AssessmentBudget(settings(), tmp_path)
    token = active_budget.set(budget)
    try:
        actual = focus_cost_reader.build_focus_cost_data({BLOB: path}, [SUB])
        assert actual == expected
        assert budget.used["csv_rows"] == 3
        assert actual.tag_spend["Late"]["Unallocated"] == 0.02
    finally:
        active_budget.reset(token)


def test_scalable_resource_costs_are_complete_while_legacy_keeps_its_top_200():
    content = io.StringIO()
    write_focus(content, rows=250, resources=250)
    parsed = focus_cost_reader.build_focus_cost_data({BLOB: gzip.compress(content.getvalue().encode())}, [SUB])
    arguments = dict(
        subscription_ids=[SUB], subscription_names=parsed.subscription_names,
        per_sub_spend=parsed.effective_cost_by_subscription, resource_graph_rows={},
        cost_by_resource_id=parsed.effective_cost_by_resource_id, service_family_spend=parsed.service_family_spend,
        advisor_recommendations=[], untagged_counts={}, focus_cost_data=parsed,
    )
    legacy = build_full_report(**arguments)
    scalable = build_full_report(**arguments, include_all_resource_costs=True)
    assert len(legacy.cost_hierarchy) == 200
    assert len(scalable.cost_hierarchy) == 250
    assert legacy.executive_summary.current_monthly_spend == scalable.executive_summary.current_monthly_spend == 2.5


def test_chunk_download_never_uses_readall_and_stops_at_byte_limit(monkeypatch, tmp_path):
    downloader = SimpleNamespace(chunks=lambda: iter([b"abc", b"def"]),
                                 readall=MagicMock(side_effect=AssertionError("readall must not be used")))
    container = SimpleNamespace(
        list_blobs=lambda **kwargs: [SimpleNamespace(name=BLOB, size=6)],
        download_blob=lambda name: downloader,
    )
    monkeypatch.setattr(focus_cost_reader.focus_export_download, "get_container_client", lambda: container)
    monkeypatch.setattr(focus_cost_reader, "_run_prefix", lambda *args: "prefix/")
    token = active_budget.set(AssessmentBudget(settings(), tmp_path))
    try:
        files = focus_cost_reader._download_run_files([SUB], {SUB: {"name": "run"}}, "period")
        assert files[BLOB].read_bytes() == b"abcdef"
        downloader.readall.assert_not_called()
    finally:
        active_budget.reset(token)
    token = active_budget.set(AssessmentBudget(replace(settings(), input_bytes=5), tmp_path))
    try:
        with pytest.raises(AssessmentLimitError, match="input_bytes"):
            focus_cost_reader._download_run_files([SUB], {SUB: {"name": "run"}}, "period")
    finally:
        active_budget.reset(token)


def test_row_budget_stops_stream_before_processing_more_records(tmp_path):
    path = tmp_path / "focus.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        write_focus(stream)
    token = active_budget.set(AssessmentBudget(replace(settings(), csv_rows=2), tmp_path))
    try:
        with pytest.raises(AssessmentLimitError, match="csv_rows"):
            focus_cost_reader.build_focus_cost_data({BLOB: path}, [SUB])
    finally:
        active_budget.reset(token)


def test_tag_spooling_has_an_independent_disk_write_budget(tmp_path):
    token = active_budget.set(AssessmentBudget(replace(settings(), spool_bytes=5), tmp_path))
    try:
        with (tmp_path / "tags.jsonl").open("w+", encoding="utf-8") as stream:
            rows = focus_cost_reader._TagRows(stream)
            from decimal import Decimal
            with pytest.raises(AssessmentLimitError, match="spool_bytes"):
                rows.append((SUB, "rg", {"team": "synthetic"}, Decimal("1.0")))
            assert stream.tell() == 0
    finally:
        active_budget.reset(token)


def test_api_mode_cannot_parse_focus_or_history(monkeypatch):
    from services import focus_history_reader
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    for operation in (focus_cost_reader.load_latest_complete_focus_costs,
                      focus_history_reader.load_complete_focus_history,
                      focus_history_reader.load_available_focus_history):
        with pytest.raises(AssessmentLimitError, match="worker-only"):
            asyncio.run(operation([SUB]))


def test_snapshot_worker_does_not_publish_compatibility_pointers(monkeypatch):
    container = SimpleNamespace(upload_blob=MagicMock())
    monkeypatch.setattr(report_snapshots, "get_container_client", lambda: container)
    snapshot = report_snapshots.build_snapshot(make_report(), [SUB], 90)
    asyncio.run(report_snapshots.publish_snapshot(snapshot, update_pointers=False))
    names = [call.args[0] for call in container.upload_blob.call_args_list]
    assert len(names) == 2
    assert all(not name.endswith("latest.json") for name in names)


def test_oversized_snapshot_is_rejected_before_download(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.delenv("ASSESSMENT_WORKER_PROCESS", raising=False)
    blob = SimpleNamespace(
        get_blob_properties=lambda: SimpleNamespace(size=settings().snapshot_read_bytes + 1),
        download_blob=MagicMock(side_effect=AssertionError("Oversized snapshot must not be downloaded")),
    )
    container = SimpleNamespace(get_blob_client=lambda name: blob)
    with pytest.raises(AssessmentLimitError, match="interactive read budget"):
        report_snapshots._download_bytes(container, "snapshot.json")
    blob.download_blob.assert_not_called()


def test_interactive_snapshot_budget_failure_never_falls_back_to_partial_report(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setattr(assessment_store, "snapshot_view", AsyncMock(
        return_value={"subscriptionIds": [SUB], "scopeHash": "scope", "report": {}, "collections": []}))
    monkeypatch.setattr(report_snapshots, "load_snapshot", AsyncMock(
        side_effect=AssessmentLimitError("Report exceeds interactive read budget")))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get("/api/assessments/snapshots/snapshot/interactive")
            assert response.status_code == 413
            assert "interactive read budget" in response.json()["detail"]
    asyncio.run(exercise())


def test_classic_row_budget_stops_large_report_before_blob_loading(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setattr(assessment_store, "snapshot_view", AsyncMock(return_value={
        "subscriptionIds": [SUB], "scopeHash": "scope", "report": {},
        "collections": [{"path": "costHierarchy", "total": 15000}],
    }))
    load = AsyncMock(side_effect=AssertionError("Large report must stay paginated"))
    monkeypatch.setattr(report_snapshots, "load_snapshot", load)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get("/api/assessments/snapshots/snapshot/interactive")
            assert response.status_code == 413
            assert "row budget" in response.json()["detail"]
    asyncio.run(exercise())
    load.assert_not_called()


def test_pool_renews_tokens_per_connection_and_requires_verified_tls(monkeypatch):
    captured = {}
    credential = SimpleNamespace(get_token=AsyncMock(side_effect=[
        SimpleNamespace(token="synthetic-first"), SimpleNamespace(token="synthetic-refreshed"),
    ]), close=AsyncMock())
    fake_pool = SimpleNamespace(close=AsyncMock())

    async def create_pool(**kwargs):
        captured.update(kwargs)
        return fake_pool

    monkeypatch.setattr(assessment_store, "_pool", None)
    monkeypatch.setattr(assessment_store, "_pool_lock", None)
    monkeypatch.setattr(assessment_store, "_credential", credential)
    monkeypatch.setattr(assessment_store.asyncpg, "create_pool", create_pool)
    for key, value in {"HOST": "private.postgres.database.azure.com", "NAME": "meghkosha", "USER": "meghkosha_api"}.items():
        monkeypatch.setenv(f"ASSESSMENT_DB_{key}", value)

    async def exercise():
        await assessment_store.pool()
        assert await captured["password"]() == "synthetic-first"
        assert await captured["password"]() == "synthetic-refreshed"
        assert captured["ssl"].check_hostname
        assert captured["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert captured["max_size"] == 2
        await assessment_store.pool(for_read=True)
        assert captured["max_size"] == 3
        credential.get_token.assert_awaited_with(assessment_store.TOKEN_SCOPE)
        await assessment_store.close()
    asyncio.run(exercise())


def test_post_report_enqueues_without_calling_assessment_builder(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    queued = {"jobId": "11111111-2222-3333-4444-555555555555", "status": "queued"}
    enqueue = AsyncMock(return_value=queued)
    monkeypatch.setattr(assessment_store, "enqueue", enqueue)
    build = AsyncMock(side_effect=AssertionError("HTTP must not build reports"))
    monkeypatch.setattr(main, "_build_and_publish_report", build)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/report", json={"subscriptionIds": [SUB], "staleDays": 90})
            assert response.status_code == 202
            assert response.headers["location"].endswith(queued["jobId"])
            assert response.json() == queued
    asyncio.run(exercise())
    build.assert_not_called()
    assert enqueue.await_args.args[0] == TENANT


def test_unauthorized_enqueue_does_not_touch_database(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    enqueue = AsyncMock()
    monkeypatch.setattr(assessment_store, "enqueue", enqueue)

    async def deny(*args):
        raise HTTPException(status_code=403, detail="No subscription access")

    monkeypatch.setattr(main.access_control, "require_all_subscription_access", deny)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/report", json={"subscriptionIds": [SUB], "staleDays": 90})
            assert response.status_code == 403
    asyncio.run(exercise())
    enqueue.assert_not_called()


def test_api_request_budget_and_queue_backpressure_are_explicit(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    enqueue = AsyncMock(side_effect=assessment_store.QueueFullError("Queue is full"))
    monkeypatch.setattr(assessment_store, "enqueue", enqueue)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            large = await client.post("/api/report", content=b"x" * 65537)
            assert large.status_code == 413
            full = await client.post("/api/report", json={"subscriptionIds": [SUB], "staleDays": 90})
            assert full.status_code == 429
            assert full.headers["retry-after"] == "30"
    asyncio.run(exercise())
    assert enqueue.await_count == 1


def test_http_admission_bounds_concurrent_requests_and_returns_retryable_overload(monkeypatch):
    from services.assessment_http import AssessmentHTTPBudget
    from starlette.responses import JSONResponse

    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("ASSESSMENT_API_CONCURRENCY", "3")
    settings.cache_clear()

    async def exercise():
        gate = asyncio.Event()
        admitted = asyncio.Event()
        active = 0
        peak = 0

        async def service(scope, receive, send):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                admitted.set()
            try:
                await gate.wait()
                await JSONResponse({"status": "ok"})(scope, receive, send)
            finally:
                active -= 1

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=AssessmentHTTPBudget(service)), base_url="http://test",
        ) as client:
            requests = [asyncio.create_task(client.get("/api/report/latest")) for _ in range(100)]
            await asyncio.wait_for(admitted.wait(), timeout=2)
            await asyncio.sleep(0.2)
            gate.set()
            responses = await asyncio.gather(*requests)
        assert peak == 3
        assert sum(response.status_code == 200 for response in responses) == 3
        assert sum(response.status_code == 429 for response in responses) == 97
        assert all(response.headers["retry-after"] == "2" for response in responses if response.status_code == 429)
    asyncio.run(exercise())


def test_missing_database_is_unready_but_does_not_kill_liveness(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setattr(assessment_store, "pool", AsyncMock(
        side_effect=assessment_store.DatabaseConfigurationError("Not configured")))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/api/ready")).status_code == 503
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get(f"/api/report/latest?subscriptionId={SUB}")).status_code == 503
    asyncio.run(exercise())


def test_lost_lease_terminates_only_the_owned_worker_subprocess(monkeypatch):
    class Process:
        returncode = None

        def __init__(self):
            self.done = asyncio.Event()
            self.terminated = False

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self.done.set()

        def kill(self):
            self.terminate()

    async def exercise():
        process = Process()
        workspace = {}

        async def spawn(*args, env):
            path = Path(env["ASSESSMENT_WORKSPACE"])
            assert path.is_dir()
            (path / "partial-export.csv").write_text("synthetic", encoding="utf-8")
            workspace["path"] = path
            return process

        monkeypatch.setattr(assessments.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(assessments.assessment_store, "heartbeat", AsyncMock(return_value=False))
        monkeypatch.setattr(assessments, "settings", lambda: replace(settings(), lease_seconds=1))
        await assessments.supervise({"job_id": "job", "lease_token": "lease"})
        assert process.terminated
        assert not workspace["path"].exists()
    asyncio.run(exercise())
