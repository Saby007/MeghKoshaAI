import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

os.environ.setdefault("AI_PROJECT_ENDPOINT", "https://example.test/api/projects/test")

from agents import cost_agent


def test_narrate_parses_json_without_request_response_format(monkeypatch):
    from azure.identity import ManagedIdentityCredential

    class FakeAgent:
        def __init__(self, **kwargs):
            assert isinstance(kwargs["credential"], ManagedIdentityCredential)

        async def run(self, prompt, **kwargs):
            assert kwargs == {}
            assert cost_agent.QUALITATIVE_NARRATION_INSTRUCTION in prompt
            assert '"monthly_total":125.0' in prompt
            assert '"impact_type":"potential_savings"' in prompt
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "executive_summary": "Two disks can be removed.",
                        "prioritized_findings": [
                            {
                                "category": "unattached_disks",
                                "priority": "High",
                                "narrative": "Remove the unattached disks after validation.",
                            }
                        ],
                    }
                )
            )

    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "true")
    monkeypatch.setenv("AZURE_CLIENT_ID", "55555555-5555-5555-5555-555555555555")
    monkeypatch.setattr(cost_agent, "_create_foundry_agent", FakeAgent)
    report = cost_agent.CostFindingsReport(
        subscriptions=["subscription-1"],
        total_monthly_spend=1000.0,
        tier_a_categories=[
            cost_agent.FindingCategorySummary(
                category="unattached_disks",
                display_name="Unattached managed disks",
                count=2,
                monthly_total=125.0,
                annual_total=1500.0,
                impact_type="potential_savings",
            )
        ],
    )

    result = asyncio.run(cost_agent.narrate(report))

    assert result is not None
    assert result.executive_summary == "Two disks can be removed."
    assert result.prioritized_findings[0].priority == "High"


def test_core_starts_without_foundry_endpoint_or_loading_agent_sdk():
    environment = dict(os.environ)
    for name in ("AI_PROJECT_ENDPOINT", "AI_SERVICES_ENDPOINT", "MODEL_ROUTER_DEPLOYMENT_NAME", "MEGHKOSHA_AI_ENABLED", "AZURE_CLIENT_ID"):
        environment.pop(name, None)
    code = (
        "import asyncio,json,sys,pytest_socket; "
        "pytest_socket.socket_allow_hosts(['127.0.0.1','::1']); "
        "import main; "
        "assert 'agent_framework.foundry' not in sys.modules; "
        "assert 'agent_framework.openai' not in sys.modules; "
        "print(json.dumps(asyncio.run(main.health())))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "ok", "features": {"aiNarration": "disabled"}}


def test_disabled_ai_is_explicit_and_does_not_instantiate_an_agent(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MEGHKOSHA_AI_ENABLED", raising=False)
    monkeypatch.delenv("AI_PROJECT_ENDPOINT", raising=False)
    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["features"]["aiNarration"] == "disabled"
        response = client.post("/api/narrate", json={})
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]
    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "true")
    assert cost_agent.narration_status() == "unconfigured"


def test_narrate_rejects_hallucinated_quantitative_values(monkeypatch):
    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, prompt, **kwargs):
            return SimpleNamespace(text=json.dumps({
                "executive_summary": "Savings of $500 are available.",
                "prioritized_findings": [
                    {"category": "unattached_disks", "priority": "High", "narrative": "Remove them."}
                ],
            }))

    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "true")
    monkeypatch.setenv("AZURE_CLIENT_ID", "55555555-5555-5555-5555-555555555555")
    monkeypatch.setattr(cost_agent, "_create_foundry_agent", FakeAgent)
    report = cost_agent.CostFindingsReport(
        subscriptions=["subscription-1"],
        total_monthly_spend=1000.0,
        tier_a_categories=[
            cost_agent.FindingCategorySummary(
                category="unattached_disks", display_name="Unattached managed disks",
                count=2, monthly_total=125.0, annual_total=1500.0,
            )
        ],
    )

    with pytest.raises(ValueError, match="prohibited quantitative value"):
        asyncio.run(cost_agent.narrate(report))


def test_cost_findings_report_rejects_oversized_input():
    with pytest.raises(ValidationError):
        cost_agent.CostFindingsReport(subscriptions=["s"] * 201, total_monthly_spend=1.0)
    with pytest.raises(ValidationError):
        cost_agent.CostFindingsReport(subscriptions=["s"], total_monthly_spend=-1.0)