from types import SimpleNamespace

import asyncio
from types import SimpleNamespace

from services import chat_model
from services.chat_model import ModelRouterNarrative, _NUMBER_OR_CURRENCY, _qualitative_only
from services.chat_responder import answer_question, respond_to_question


def _report():
    months = []
    for index in range(1, 8):
        compute = 60 + index * 10
        storage = 40 + index * 5
        months.append(SimpleNamespace(
            month=f"2026-{index:02d}",
            total=compute + storage,
            subscription_spend={"sub-1": compute + storage},
            subscription_category_spend={"sub-1": {"Compute": compute, "Storage": storage}},
        ))
    disk = SimpleNamespace(
        resource_name="disk-one",
        resource_id="/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/disks/disk-one",
        subscription_name="Subscription One",
        subscription_id="sub-1",
        monthly_cost=12.5,
        detail="Disk has no managedBy attachment.",
    )
    return SimpleNamespace(
        report_metadata=SimpleNamespace(currency="USD", period="2026-07", cost_basis="FOCUS EffectiveCost"),
        spend_history=SimpleNamespace(months=months, status_message="7 complete months available."),
        advisor_score=SimpleNamespace(cost_score=82.0, score=80.0, monthly_change=2.0, status="Available"),
        subscription_breakdown=[SimpleNamespace(
            subscription_id="sub-1",
            subscription_name="Subscription One",
            current_spend=205.0,
            total_waste=12.5,
            category_costs={"Compute": 130.0, "Storage": 75.0},
        )],
        tier_a_categories=[SimpleNamespace(category="unattached_disks", lines=[disk])],
        prioritized_findings=[],
        spend_categories=[],
    )


def test_overview_includes_score_trends_and_forecast_without_model_output():
    answer = answer_question("Show my FinOps overview with trends and forecast", _report())

    assert answer.intent == "overview"
    assert [metric.label for metric in answer.metrics] == [
        "FinOps Score",
        "3 month trend",
        "6 month trend",
        "12 month trend",
        "Expected next month",
        "2026 projection",
    ]
    assert answer.metrics[0].value == "82 / 100"
    assert "no Foundry model call" in answer.disclaimer


def test_subscription_change_names_largest_category_increase():
    answer = answer_question("Why is subscription Subscription One spending more this month?", _report())

    assert answer.intent == "subscription_change"
    assert "increased by USD 15.00" in answer.answer
    assert "Compute (USD 10.00)" in answer.answer
    assert answer.data_as_of == "2026-07"


def test_unattached_disks_returns_grounded_resource_lines():
    answer = answer_question("Show all unattached disks", _report())

    assert answer.intent == "unattached_disks"
    assert answer.metrics[0].value == "1"
    assert answer.resources[0].resource_name == "disk-one"
    assert answer.resources[0].monthly_cost == 12.5


def test_model_router_contract_rejects_quantitative_narrative():
    assert _NUMBER_OR_CURRENCY.search("Spend increased by 12 percent.")
    assert _NUMBER_OR_CURRENCY.search("Spend is $12 higher.")
    assert _NUMBER_OR_CURRENCY.search("The latest period is 2026-07.")
    assert not _NUMBER_OR_CURRENCY.search("Compute is the main driver; review verified metrics below.")


def test_model_router_input_replaces_quantitative_values():
    sanitized = _qualitative_only({
        "question": "Show 3, 6, and 12 month trends for USD 12.50.",
        "value": 12.5,
    })

    assert not _NUMBER_OR_CURRENCY.search(sanitized["question"])
    assert sanitized["value"] == "[verified value]"


def test_response_falls_back_when_model_router_is_not_configured(monkeypatch):
    monkeypatch.delenv("AI_SERVICES_ENDPOINT", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_DEPLOYMENT_NAME", raising=False)

    answer = asyncio.run(respond_to_question("Show all unattached disks", _report()))

    assert answer.response_mode == "deterministic_fallback"
    assert answer.selected_model is None
    assert "Model Router was unavailable" in answer.disclaimer


def test_model_router_narrative_contract_uses_evidence_alias():
    value = ModelRouterNarrative(answer="Compute is the main driver.", evidenceKeys=["spend.Compute"])

    assert value.evidence_keys == ["spend.Compute"]


def test_model_router_response_exposes_selected_model_and_usage(monkeypatch):
    from azure.identity import ManagedIdentityCredential

    class FakeAgent:
        async def run(self, prompt, options):
            assert "evidenceCatalog" in prompt
            assert options["response_format"] is ModelRouterNarrative
            return SimpleNamespace(
                value=ModelRouterNarrative(
                    answer="Compute is the primary driver; review the verified trend evidence.",
                    evidenceKeys=["verified.answer"],
                ),
                usage_details={
                    "input_token_count": 120,
                    "output_token_count": 20,
                    "total_token_count": 140,
                },
                raw_representation=SimpleNamespace(model="gpt-4.1-mini"),
            )

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["model"] == "finops-model-router"
            assert isinstance(kwargs["credential"], ManagedIdentityCredential)

        def as_agent(self, **kwargs):
            return FakeAgent()

    monkeypatch.setenv("AI_SERVICES_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("MODEL_ROUTER_DEPLOYMENT_NAME", "finops-model-router")
    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "true")
    monkeypatch.setenv("AZURE_CLIENT_ID", "55555555-5555-5555-5555-555555555555")
    monkeypatch.setattr(chat_model, "_create_chat_client", FakeClient)

    answer = asyncio.run(respond_to_question("Show all unattached disks", _report()))

    assert answer.response_mode == "model_router"
    assert answer.selected_model == "gpt-4.1-mini"
    assert answer.usage and answer.usage.total_tokens == 140
    assert answer.evidence_keys == ["verified.answer"]


def test_disabled_ai_does_not_use_configured_router(monkeypatch):
    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "false")
    monkeypatch.setenv("AI_SERVICES_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("MODEL_ROUTER_DEPLOYMENT_NAME", "finops-model-router")

    def unexpected_client(**kwargs):
        raise AssertionError("Disabled AI tried to instantiate a client")

    monkeypatch.setattr(chat_model, "_create_chat_client", unexpected_client)
    monkeypatch.setattr(chat_model.runtime_identity, "credential", unexpected_client)
    answer = asyncio.run(respond_to_question("Show all unattached disks", _report()))
    assert answer.response_mode == "deterministic_fallback"