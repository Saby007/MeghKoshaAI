"""Optional Foundry narration of precomputed findings; disabled in the identity core."""

import os
import re

from pydantic import BaseModel, Field

from services import runtime_identity

QUALITATIVE_NARRATION_INSTRUCTION = (
    "Write qualitative narration only. Do not include currency symbols, monetary "
    "amounts, or restated monthly/annual totals in the response."
)

_NUMBER_OR_CURRENCY = re.compile(r"[0-9$€£¥]")


class FindingCategorySummary(BaseModel):
    category: str = Field(max_length=200)
    display_name: str = Field(max_length=200)
    count: int = Field(ge=0, le=1_000_000)
    monthly_total: float = Field(ge=0, le=1_000_000_000)
    annual_total: float = Field(ge=0, le=1_000_000_000)
    impact_type: str = Field(default="potential_savings", max_length=50)


class CostFindingsReport(BaseModel):
    subscriptions: list[str] = Field(max_length=200)
    total_monthly_spend: float = Field(ge=0, le=1_000_000_000)
    tier_a_categories: list[FindingCategorySummary] = Field(default_factory=list, max_length=50)

    @property
    def has_findings(self) -> bool:
        return len(self.tier_a_categories) > 0


class PrioritizedFinding(BaseModel):
    category: str
    priority: str
    narrative: str


class CostAgentOutput(BaseModel):
    executive_summary: str
    prioritized_findings: list[PrioritizedFinding]


def narration_status() -> str:
    if os.environ.get("MEGHKOSHA_AI_ENABLED", "false").strip().lower() != "true":
        return "disabled"
    return "configured" if os.environ.get("AI_PROJECT_ENDPOINT", "").strip() else "unconfigured"


def _create_foundry_agent(**kwargs):
    from agent_framework.foundry import FoundryAgent

    return FoundryAgent(**kwargs)


async def narrate(report: CostFindingsReport) -> CostAgentOutput | None:
    if narration_status() != "configured" or not report.has_findings:
        return None
    with runtime_identity.credential() as credential:
        agent = _create_foundry_agent(
            project_endpoint=os.environ["AI_PROJECT_ENDPOINT"],
            agent_name=os.environ.get("AGENT_NAME", "cost-agent"),
            credential=credential,
        )
        result = await agent.run(
            f"{QUALITATIVE_NARRATION_INSTRUCTION}\n\nFindings JSON:\n{report.model_dump_json()}"
        )
    narrative = CostAgentOutput.model_validate_json(result.text)
    if _NUMBER_OR_CURRENCY.search(narrative.executive_summary) or any(
        _NUMBER_OR_CURRENCY.search(finding.narrative) for finding in narrative.prioritized_findings
    ):
        raise ValueError("Foundry narration contained a prohibited quantitative value")
    return narrative


