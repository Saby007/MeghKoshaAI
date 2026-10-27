"""Compact deterministic evidence for Chat, independent of full report payload size."""

import heapq
from itertools import islice

from pydantic import BaseModel, ConfigDict, Field

from findings.models import FindingCategorySummary
from reports.models import (
    AdvisorScoreSummary, CostHierarchyItem, DataCompleteness, ExecutiveFinding,
    FullReport, ReportMetadata, SpendCategorySummary, SpendHistorySummary, SubscriptionBreakdownRow,
)

CHAT_RESOURCE_LIMIT = 25
CHAT_EVIDENCE_BYTES = 262144


class ChatEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    report_metadata: ReportMetadata = Field(alias="reportMetadata")
    completeness: DataCompleteness
    advisor_score: AdvisorScoreSummary = Field(alias="advisorScore")
    spend_history: SpendHistorySummary = Field(alias="spendHistory")
    subscription_breakdown: list[SubscriptionBreakdownRow] = Field(alias="subscriptionBreakdown")
    prioritized_findings: list[ExecutiveFinding] = Field(alias="prioritizedFindings")
    spend_categories: list[SpendCategorySummary] = Field(alias="spendCategories")
    cost_hierarchy: list[CostHierarchyItem] = Field(alias="costHierarchy", max_length=CHAT_RESOURCE_LIMIT)
    tier_a_categories: list[FindingCategorySummary] = Field(alias="tierACategories", max_length=1)
    unattached_disk_count: int = Field(alias="unattachedDiskCount", ge=0)
    unattached_disk_monthly_cost: float = Field(alias="unattachedDiskMonthlyCost")
    coverage_note: str = Field(alias="coverageNote")


ChatReport = FullReport | ChatEvidence


def build_chat_evidence(report: FullReport) -> ChatEvidence:
    disks = next((item for item in report.tier_a_categories if item.category == "unattached_disks"), None)
    lines = disks.lines if disks else []
    categories = []
    if disks is not None:
        bounded = [
            line.model_copy(update={
                "cost_evidence": [], "focus_pricing_evidence": [], "sql_context": None, "idle_evidence": None,
            }) for line in islice(lines, CHAT_RESOURCE_LIMIT)
        ]
        categories.append(disks.model_copy(update={"lines": bounded, "remediation": None}))
    top = heapq.nsmallest(
        CHAT_RESOURCE_LIMIT,
        (row for row in report.cost_hierarchy if row.monthly_spend > 0),
        key=lambda row: (-row.monthly_spend, row.resource_name.casefold(), row.resource_id.casefold()),
    )
    return ChatEvidence(
        reportMetadata=report.report_metadata, completeness=report.completeness,
        advisorScore=report.advisor_score, spendHistory=report.spend_history,
        subscriptionBreakdown=report.subscription_breakdown,
        prioritizedFindings=report.prioritized_findings[:10], spendCategories=report.spend_categories,
        costHierarchy=top, tierACategories=categories,
        unattachedDiskCount=len(lines), unattachedDiskMonthlyCost=sum(line.monthly_cost or 0 for line in lines),
        coverageNote=(
            "Grounded in the complete completed assessment. Monthly and subscription totals cover the full scope. "
            "Top-resource rankings are exact up to the displayed limit; unattached-disk totals cover all findings, "
            "but at most 25 resource details are shown. Use the paginated report for the full listing."
        ),
    )
