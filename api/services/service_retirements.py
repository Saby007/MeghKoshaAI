"""Resource-specific retirement notices from Azure Advisor's published contract."""

import asyncio
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode

from openpyxl import Workbook
from pydantic import BaseModel, ConfigDict, Field

from reports.exports import ReportArtifact, _sheet
from reports.models import ReportSnapshot
from services import arm_client

_cache: OrderedDict[tuple[str, ...], tuple[float, "RetirementSummary"]] = OrderedDict()
_semaphore = asyncio.Semaphore(4)


class RetirementNotice(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation_id: str = Field(alias="recommendationId")
    subscription_id: str = Field(alias="subscriptionId")
    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    resource_type: str = Field(alias="resourceType")
    feature: str
    retirement_date: str | None = Field(alias="retirementDate")
    guidance: str
    matched_cost: float | None = Field(alias="matchedCost")


class RetirementSourceStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    available: bool
    message: str


class RetirementSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    snapshot_id: str = Field(alias="snapshotId")
    observed_at: str = Field(alias="observedAt")
    cost_period: str = Field(alias="costPeriod")
    currency: str
    notices: list[RetirementNotice]
    sources: list[RetirementSourceStatus]


def retirement_notices(snapshot: ReportSnapshot, subscription_id: str, items: list[dict]) -> list[RetirementNotice]:
    notices: dict[str, RetirementNotice] = {}
    for item in items:
        properties = item.get("properties") or {}
        extended = properties.get("extendedProperties") or {}
        if extended.get("recommendationSubCategory", extended.get("recommendationControl")) != "ServiceUpgradeAndRetirement":
            continue
        feature = extended.get("retirementFeatureName")
        if not isinstance(feature, str) or feature.strip().casefold() in {"", "n/a"}:
            continue
        resource = properties.get("resourceMetadata") or {}
        resource_id = resource.get("resourceId") or resource.get("ResourceId")
        if not isinstance(resource_id, str) or not resource_id.casefold().startswith(f"/subscriptions/{subscription_id}/".casefold()):
            raise ValueError("Retirement source resource scope could not be verified")
        raw_date = extended.get("retirementDate")
        try:
            retirement_date = str(date.fromisoformat(raw_date)) if raw_date else None
        except (ValueError, TypeError):
            retirement_date = None
        details = snapshot.report.cost_details
        metadata = snapshot.report.report_metadata
        costs = [row for row in details.rows if row.resource_id.casefold() == resource_id.casefold()]
        matched_cost = None
        if details.status == "complete" and costs:
            start, end = date.fromisoformat(metadata.period_start), date.fromisoformat(metadata.period_end)
            period_dates = {str(start + timedelta(days=index)) for index in range((end - start).days + 1)}
            if period_dates.issubset(details.dates):
                matched_cost = sum(amount for row in costs for day, amount in row.daily_costs.items() if day in period_dates)
        else:
            matched = [row.monthly_spend for row in snapshot.report.cost_hierarchy if row.resource_id.casefold() == resource_id.casefold()]
            if matched:
                matched_cost = sum(matched)
        recommendation_id = str(item.get("id") or properties.get("id") or f"{resource_id}:{properties.get('recommendationTypeId', feature)}")
        notices[recommendation_id] = RetirementNotice(
            recommendationId=recommendation_id, subscriptionId=subscription_id, resourceId=resource_id,
            resourceName=str(properties.get("impactedValue") or resource_id.rsplit("/", 1)[-1]),
            resourceType=str(properties.get("impactedField") or ""), feature=feature,
            retirementDate=retirement_date, guidance=str((properties.get("shortDescription") or {}).get("solution") or "Review this retirement in Azure Advisor."),
            matchedCost=matched_cost,
        )
    return list(notices.values())


async def get_retirements(snapshot: ReportSnapshot, subscription_id: str | None = None) -> RetirementSummary:
    subscriptions = sorted(set(value.lower() for value in snapshot.subscription_ids))
    if subscription_id:
        if subscription_id.lower() not in subscriptions:
            raise ValueError("Subscription is outside this report")
        subscriptions = [subscription_id.lower()]
    if len(subscriptions) > 25:
        raise ValueError("Select one subscription for retirement checks on reports larger than 25 subscriptions")
    key = (snapshot.snapshot_id, *subscriptions)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        _cache.move_to_end(key)
        return cached[1]
    query = urlencode({"api-version": "2025-01-01", "$filter": "Category eq 'HighAvailability' and SubCategory eq 'ServiceUpgradeAndRetirement'"})

    async def collect(subscription: str):
        try:
            async with asyncio.timeout(15), _semaphore:
                items = await arm_client._list_arm_collection(f"/subscriptions/{subscription}/providers/Microsoft.Advisor/recommendations?{query}", timeout=8)
            return retirement_notices(snapshot, subscription, items), RetirementSourceStatus(subscriptionId=subscription, available=True, message="Resource-scoped Advisor retirements. Advisor coverage is not a complete catalog of every Azure retirement.")
        except Exception:
            return [], RetirementSourceStatus(subscriptionId=subscription, available=False, message="Azure Advisor retirement data could not be verified. Retry later.")

    results = await asyncio.gather(*(collect(subscription) for subscription in subscriptions))
    summary = RetirementSummary(
        snapshotId=snapshot.snapshot_id, observedAt=datetime.now(timezone.utc).isoformat(),
        costPeriod=snapshot.report.report_metadata.period, currency=snapshot.report.report_metadata.currency,
        notices=sorted((notice for notices, _ in results for notice in notices), key=lambda item: (item.retirement_date or "9999", item.resource_id)),
        sources=[status for _, status in results],
    )
    if all(status.available for status in summary.sources):
        _cache[key] = (time.monotonic(), summary)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return summary


def export_retirements(summary: RetirementSummary) -> ReportArtifact:
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(workbook, "Provenance", ["Field", "Value"], [
        ["Snapshot", summary.snapshot_id], ["Advisor checked", summary.observed_at],
        ["Cost period", summary.cost_period], ["Cost basis", "FOCUS EffectiveCost"], ["Currency", summary.currency],
        ["Coverage", "Only resource-specific retirements returned by Azure Advisor. Blank cost means no exact report match, not zero."],
    ])
    _sheet(workbook, "Retirements", ["Feature", "Retirement date", "Resource", "Resource ID", "Resource type", "Subscription ID", "Matched cost", "Guidance"], [
        [notice.feature, notice.retirement_date or "Not returned", notice.resource_name, notice.resource_id, notice.resource_type, notice.subscription_id, notice.matched_cost, notice.guidance]
        for notice in summary.notices
    ])
    _sheet(workbook, "Source status", ["Subscription ID", "Available", "Status"], [[item.subscription_id, item.available, item.message] for item in summary.sources])
    output = BytesIO()
    workbook.save(output)
    return ReportArtifact(output.getvalue(), "MeghKoshaAI-Service-Retirements.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")