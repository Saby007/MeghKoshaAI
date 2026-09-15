import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
from tests.test_control_api import FakeRequest


SNAPSHOT_ID = "a" * 32


@pytest.mark.parametrize("endpoint", ["chat", "export", "cost_details", "availability"])
@pytest.mark.parametrize("allowed", [True, False])
def test_selected_snapshot_is_authorized_without_substituting_latest(monkeypatch, endpoint, allowed):
    calls = []
    snapshot = SimpleNamespace(snapshot_id=SNAPSHOT_ID, subscription_ids=["sub-1"], report=SimpleNamespace(
        cost_details=SimpleNamespace(status="complete", dates=["2026-08-01"], rows=[SimpleNamespace(resource_id="/resource")]),
    ))

    async def unexpected_discovery(*args, **kwargs):
        pytest.fail("Explicit snapshot was replaced by current all-subscription discovery")

    async def load_snapshot(snapshot_id):
        assert snapshot_id == SNAPSHOT_ID
        calls.append("load")
        return snapshot

    async def authorize(object_id, subscription_ids):
        assert object_id == "22222222-2222-2222-2222-222222222222"
        assert subscription_ids == ["sub-1"]
        calls.append("authorize")
        if not allowed:
            raise HTTPException(status_code=403, detail="Access denied")

    async def build_artifact(selected, report_type, subscription_id=None):
        assert selected is snapshot
        calls.append("consume")
        return SimpleNamespace(content=b"report", media_type="application/pdf", file_name="report.pdf")

    async def respond(question, report, history):
        assert report is snapshot.report
        calls.append("consume")
        return main.chat_responder.ChatAnswer(intent="help", answer="Selected report", dataAsOf="2026-07", disclaimer="Test")

    monkeypatch.setattr(main.arm_client, "list_subscriptions", unexpected_discovery)
    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", unexpected_discovery)
    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    monkeypatch.setattr(main.access_control, "require_all_subscription_access", authorize)
    monkeypatch.setattr(main, "_build_report_artifact", build_artifact)
    monkeypatch.setattr(main.chat_responder, "respond_to_question", respond)

    def detail_artifact(selected, *args):
        assert selected is snapshot
        calls.append("consume")
        return SimpleNamespace(content=b"report", media_type="application/octet-stream", file_name="report.xlsx")

    async def availability(resource_id, billing_date):
        assert resource_id == "/resource"
        assert billing_date == date(2026, 8, 1)
        calls.append("consume")
        return SimpleNamespace(status="unavailable")

    monkeypatch.setattr(main, "build_cost_detail_export", detail_artifact)
    monkeypatch.setattr(main.resource_uptime, "get_resource_availability", availability)
    if endpoint == "chat":
        coroutine = main.chat(FakeRequest(), main.ChatRequest(question="Explain this report", snapshotId=SNAPSHOT_ID))
    elif endpoint == "cost_details":
        coroutine = main.export_cost_details(FakeRequest(), main.CostDetailExportRequest(snapshotId=SNAPSHOT_ID, startDate="2026-08-01", endDate="2026-08-01"))
    elif endpoint == "availability":
        coroutine = main.get_report_resource_availability(FakeRequest(), SNAPSHOT_ID, "/resource", date(2026, 8, 1))
    else:
        coroutine = main.export_report(FakeRequest(), SNAPSHOT_ID, "executive", None)
    if allowed:
        result = asyncio.run(coroutine)
        assert calls == ["load", "authorize", "consume"]
        if endpoint in {"export", "cost_details"}:
            assert result.headers["X-Report-Snapshot-Id"] == SNAPSHOT_ID
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(coroutine)
        assert error.value.status_code == 403
        assert calls == ["load", "authorize"]


@pytest.mark.parametrize("endpoint", ["chat", "export"])
@pytest.mark.parametrize("failure, status", [
    (main.report_snapshots.ReportSnapshotNotFoundError, 404),
    (main.report_snapshots.ReportSnapshotIntegrityError, 503),
])
def test_selected_snapshot_failures_do_not_fall_back_to_other_reports(monkeypatch, endpoint, failure, status):
    async def failed_load(snapshot_id):
        raise failure("unavailable")

    async def unexpected(*args, **kwargs):
        pytest.fail("Failed snapshot triggered a fallback or consumer")

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", failed_load)
    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", unexpected)
    monkeypatch.setattr(main.chat_responder, "respond_to_question", unexpected)
    monkeypatch.setattr(main, "_build_report_artifact", unexpected)
    coroutine = (main.chat(FakeRequest(), main.ChatRequest(question="Explain this report", snapshotId=SNAPSHOT_ID))
                 if endpoint == "chat" else main.export_report(FakeRequest(), SNAPSHOT_ID, "executive", None))
    with pytest.raises(HTTPException) as error:
        asyncio.run(coroutine)
    assert error.value.status_code == status


@pytest.mark.parametrize("resource_id,billing_date", [("/other", date(2026, 8, 1)), ("/resource", date(2026, 8, 2))])
def test_availability_rejects_resources_or_dates_not_in_the_snapshot(monkeypatch, resource_id, billing_date):
    async def load_snapshot(snapshot_id):
        return SimpleNamespace(subscription_ids=["sub-1"], report=SimpleNamespace(cost_details=SimpleNamespace(
            status="complete", dates=["2026-08-01"], rows=[SimpleNamespace(resource_id="/resource")],
        )))

    async def unexpected(*args, **kwargs):
        pytest.fail("Out-of-snapshot telemetry was queried")

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    monkeypatch.setattr(main.resource_uptime, "get_resource_availability", unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_report_resource_availability(FakeRequest(), SNAPSHOT_ID, resource_id, billing_date))
    assert error.value.status_code == 404


@pytest.mark.parametrize("allowed", [True, False])
def test_retirement_read_requires_every_snapshot_subscription_before_advisor(monkeypatch, allowed):
    calls = []
    snapshot = SimpleNamespace(subscription_ids=["sub-1", "sub-2"])

    async def load_snapshot(snapshot_id):
        assert snapshot_id == SNAPSHOT_ID
        return snapshot

    async def authorize(object_id, subscriptions):
        assert subscriptions == ["sub-1", "sub-2"]
        calls.append("authorize")
        if not allowed:
            raise HTTPException(status_code=403)

    async def retirements(selected, subscription):
        assert selected is snapshot and subscription == "sub-1"
        calls.append("advisor")
        return SimpleNamespace(model_dump=lambda **kwargs: {"notices": []})

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    monkeypatch.setattr(main.access_control, "require_all_subscription_access", authorize)
    monkeypatch.setattr(main.service_retirements, "get_retirements", retirements)
    coroutine = main.get_report_retirements(FakeRequest(), SNAPSHOT_ID, "sub-1", False)
    if allowed:
        assert asyncio.run(coroutine) == {"notices": []}
        assert calls == ["authorize", "advisor"]
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(coroutine)
        assert error.value.status_code == 403
        assert calls == ["authorize"]