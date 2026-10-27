import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import main
from findings.models import FindingCategorySummary, FindingLine
from reports.chat_evidence import CHAT_EVIDENCE_BYTES, build_chat_evidence
from services import assessment_store, chat_model, chat_responder, report_snapshots
from test_scalable_assessments import SUB, make_report


def report_with_disks(count=1000):
    report = make_report(15000)
    lines = [FindingLine(
        category="unattached_disks", resourceId=f"/subscriptions/{SUB}/disks/{index}",
        resourceName=f"disk-{index}", subscriptionId=SUB, subscriptionName="Synthetic subscription",
        monthlyCost=0.25, confidence=1, evidenceType="verified_cost", detail="No managedBy attachment.",
    ) for index in range(count)]
    report.tier_a_categories = [FindingCategorySummary(
        category="unattached_disks", displayName="Unattached disks", count=count,
        monthlyTotal=count * 0.25, annualTotal=count * 3, impactType="potential_savings", lines=lines,
    )]
    return report


def test_large_report_chat_keeps_exact_totals_with_bounded_rankings_and_listings():
    report = report_with_disks()
    report.cost_hierarchy[-1].monthly_spend = 9999
    evidence = build_chat_evidence(report)
    assert len(evidence.model_dump_json().encode()) < CHAT_EVIDENCE_BYTES
    assert len(evidence.cost_hierarchy) == 25
    assert evidence.cost_hierarchy[0].resource_name == "vm-14999"
    assert len(evidence.tier_a_categories[0].lines) == 25
    top = chat_responder.answer_question("List the top 5 resources", evidence)
    assert len(top.resources) == 5
    assert top.resources[0].monthly_cost == 9999
    disks = chat_responder.answer_question("Show all unattached disks", evidence)
    assert disks.metrics[0].value == "1000"
    assert disks.metrics[0].detail == "Matched monthly cost USD 250.00"
    assert len(disks.resources) == 25
    assert "Showing 25 of 1000" in disks.answer
    assert "paginated" in disks.disclaimer
    assert len(report.tier_a_categories[0].lines) == 1000


@pytest.mark.parametrize("question", [
    "Show score", "Show the 3, 6, 12 month trends", "Show forecast for next month",
    "Why is subscription Synthetic subscription spending more?", "Show my overview", "What can I ask?",
])
def test_compact_evidence_preserves_existing_nonresource_intents(question):
    report = make_report(200)
    legacy = chat_responder.answer_question(question, report)
    bounded = chat_responder.answer_question(question, build_chat_evidence(report))
    assert bounded.answer == legacy.answer
    assert bounded.metrics == legacy.metrics
    assert bounded.intent == legacy.intent
    assert bounded.data_as_of == legacy.data_as_of


def test_model_router_uses_bounded_postgres_evidence_and_retains_coverage(monkeypatch):
    evidence = build_chat_evidence(report_with_disks())
    prompts = []

    class Client:
        def as_agent(self, **kwargs):
            return self

        async def run(self, prompt, options):
            prompts.append(prompt)
            return SimpleNamespace(
                value=chat_model.ModelRouterNarrative(
                    answer="Review the unattached disk evidence and the complete paginated findings.",
                    evidenceKeys=["verified.answer", "snapshot.evidence_coverage"],
                ), usage_details={}, raw_representation=SimpleNamespace(model="synthetic-router-model"),
            )
    monkeypatch.setenv("FOUNDRY_CHAT_ENABLED", "true")
    monkeypatch.setenv("AI_SERVICES_ENDPOINT", "https://private.example.cognitiveservices.azure.com")
    monkeypatch.setenv("MODEL_ROUTER_DEPLOYMENT_NAME", "model-router")
    monkeypatch.setattr(chat_model, "_create_chat_client", lambda **kwargs: Client())
    monkeypatch.setenv("AZURE_CLIENT_ID", SUB)
    answer = asyncio.run(chat_responder.respond_to_question("Show all unattached disks", evidence))
    assert answer.response_mode == "model_router"
    assert answer.metrics[0].value == "1000"
    assert len(answer.resources) == 25
    assert "at most 25" in answer.disclaimer
    assert len(prompts[0].encode()) < CHAT_EVIDENCE_BYTES


def test_model_outage_preserves_verified_totals_and_bounded_coverage(monkeypatch):
    monkeypatch.setenv("FOUNDRY_CHAT_ENABLED", "false")
    answer = asyncio.run(chat_responder.respond_to_question(
        "Show all unattached disks", build_chat_evidence(report_with_disks()),
    ))
    assert answer.response_mode == "deterministic_fallback"
    assert answer.metrics[0].value == "1000"
    assert "Model Router was unavailable" in answer.disclaimer
    assert "at most 25" in answer.disclaimer


@pytest.mark.parametrize("unauthorized", [False, True])
def test_postgres_chat_never_loads_full_blob_and_checks_scope_before_evidence(monkeypatch, unauthorized):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("FOUNDRY_CHAT_ENABLED", "false")
    snapshot_id = "snapshot-large"
    monkeypatch.setattr(assessment_store, "snapshot_view", AsyncMock(
        return_value={"subscriptionIds": [SUB], "snapshotId": snapshot_id},
    ))
    load = AsyncMock(return_value=build_chat_evidence(report_with_disks()))
    blob = AsyncMock(side_effect=AssertionError("PostgreSQL Chat must not load a Blob snapshot"))
    monkeypatch.setattr(assessment_store, "chat_evidence", load)
    monkeypatch.setattr(report_snapshots, "load_snapshot", blob)
    if unauthorized:
        async def deny(*args):
            raise HTTPException(status_code=403, detail="No access")
        monkeypatch.setattr(main.access_control, "require_all_subscription_access", deny)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/chat", json={"snapshotId": snapshot_id, "question": "Show all unattached disks"})
            assert response.status_code == (403 if unauthorized else 200)
            if not unauthorized:
                assert response.json()["metrics"][0]["value"] == "1000"
                assert len(response.json()["resources"]) == 25
    asyncio.run(exercise())
    assert load.await_count == (0 if unauthorized else 1)
    blob.assert_not_called()


def test_missing_old_chat_evidence_is_explicit_and_never_uses_blob_fallback(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setattr(assessment_store, "snapshot_view", AsyncMock(return_value={"subscriptionIds": [SUB]}))
    monkeypatch.setattr(assessment_store, "chat_evidence", AsyncMock(return_value=None))
    blob = AsyncMock(side_effect=AssertionError("No unbounded fallback"))
    monkeypatch.setattr(report_snapshots, "load_snapshot", blob)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/chat", json={"snapshotId": "old-snapshot", "question": "Show overview"})
            assert response.status_code == 409
            assert "new background assessment" in response.json()["detail"]
            unbound = await client.post("/api/chat", json={"question": "Show overview"})
            assert unbound.status_code == 422
    asyncio.run(exercise())
    blob.assert_not_called()
