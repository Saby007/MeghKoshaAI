"""Normalize Azure reservation and savings-plan purchase recommendations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from reports.models import (
    RateOptimizationResponse,
    RateOptimizationScenario,
    RateRecommendation,
    RecommendationSourceStatus,
)
from services import arm_client

_RESERVATION_REVIEW_URL = "https://portal.azure.com/#view/Microsoft_Azure_Reservations/ReservationsBrowseBlade"
_SAVINGS_PLAN_REVIEW_URL = "https://portal.azure.com/#view/Microsoft_Azure_Billing/BenefitsHubBlade"
_COMPUTE_OVERLAP_TYPES = {"AppService", "RedHat", "SUSELinux", "VirtualMachines"}


def _optional_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _reservation_recommendation(
    item: dict,
    subscription_id: str,
    subscription_name: str,
    scenario: RateOptimizationScenario,
) -> RateRecommendation:
    properties = item.get("properties") or {}
    cost_without = _optional_float(properties.get("costWithNoReservedInstances"))
    projected_savings = _optional_float(properties.get("netSavings"))
    resource_type = str(properties.get("resourceType") or scenario.reservation_resource_type)
    overlap_group = (
        f"{subscription_id}:compute" if resource_type in _COMPUTE_OVERLAP_TYPES else ""
    )
    return RateRecommendation(
        kind="reservation",
        recommendation_id=str(item.get("id") or item.get("name") or ""),
        subscription_id=subscription_id,
        subscription_name=subscription_name,
        sku=str(item.get("sku") or properties.get("normalizedSize") or ""),
        region=str(item.get("location") or ""),
        quantity=_optional_float(properties.get("recommendedQuantity")),
        hourly_commitment=None,
        commitment_granularity="",
        term=str(properties.get("term") or scenario.term),
        look_back_period=str(properties.get("lookBackPeriod") or scenario.look_back_period),
        scope=str(properties.get("scope") or scenario.scope),
        resource_type=resource_type,
        currency=str(properties.get("billingCurrency") or properties.get("currency") or ""),
        cost_without_benefit=cost_without,
        cost_with_benefit=_optional_float(properties.get("totalCostWithReservedInstances")),
        projected_savings=projected_savings,
        savings_percentage=_ratio(projected_savings, cost_without),
        coverage_percentage=None,
        utilization_percentage=None,
        wastage_cost=None,
        first_usage_date=str(properties.get("firstUsageDate") or ""),
        last_usage_date=str(properties.get("lastUsageDate") or ""),
        total_hours=_optional_int(properties.get("totalHours")),
        overlap_group=overlap_group,
        source="Microsoft.Consumption/reservationRecommendations 2024-08-01",
        review_url=_RESERVATION_REVIEW_URL,
    )


def _savings_plan_recommendation(
    item: dict,
    subscription_id: str,
    subscription_name: str,
    scenario: RateOptimizationScenario,
) -> RateRecommendation:
    properties = item.get("properties") or {}
    details = properties.get("recommendationDetails") or {}
    return RateRecommendation(
        kind="savings_plan",
        recommendation_id=str(item.get("id") or item.get("name") or ""),
        subscription_id=subscription_id,
        subscription_name=subscription_name,
        sku=str(properties.get("armSkuName") or item.get("kind") or "SavingsPlan"),
        region=str(item.get("location") or ""),
        quantity=None,
        hourly_commitment=_optional_float(details.get("commitmentAmount")),
        commitment_granularity=str(properties.get("commitmentGranularity") or ""),
        term=str(properties.get("term") or scenario.term),
        look_back_period=str(properties.get("lookBackPeriod") or scenario.look_back_period),
        scope=str(properties.get("scope") or scenario.scope),
        resource_type="Compute",
        currency=str(properties.get("currencyCode") or ""),
        cost_without_benefit=_optional_float(properties.get("costWithoutBenefit")),
        cost_with_benefit=_optional_float(details.get("totalCost")),
        projected_savings=_optional_float(details.get("savingsAmount")),
        savings_percentage=(
            value / 100 if (value := _optional_float(details.get("savingsPercentage"))) is not None else None
        ),
        coverage_percentage=(
            value / 100 if (value := _optional_float(details.get("coveragePercentage"))) is not None else None
        ),
        utilization_percentage=(
            value / 100
            if (value := _optional_float(details.get("averageUtilizationPercentage"))) is not None
            else None
        ),
        wastage_cost=_optional_float(details.get("wastageCost")),
        first_usage_date=str(properties.get("firstConsumptionDate") or ""),
        last_usage_date=str(properties.get("lastConsumptionDate") or ""),
        total_hours=_optional_int(properties.get("totalHours")),
        overlap_group=f"{subscription_id}:compute",
        source="Microsoft.CostManagement/benefitRecommendations 2026-06-01",
        review_url=_SAVINGS_PLAN_REVIEW_URL,
    )


async def collect_rate_optimization(
    subscription_ids: list[str],
    subscription_names: dict[str, str],
    scenario: RateOptimizationScenario,
) -> RateOptimizationResponse:
    async def collect_subscription(subscription_id: str):
        return await asyncio.gather(
            arm_client.list_reservation_recommendations(
                subscription_id,
                scenario.look_back_period,
                scenario.term,
                scenario.reservation_resource_type,
            ),
            arm_client.list_savings_plan_recommendations(
                subscription_id,
                scenario.look_back_period,
                scenario.term,
            ),
            return_exceptions=True,
        )

    results = await asyncio.gather(*(collect_subscription(value) for value in subscription_ids))
    reservations: list[RateRecommendation] = []
    savings_plans: list[RateRecommendation] = []
    statuses: list[RecommendationSourceStatus] = []

    for subscription_id, (reservation_result, savings_plan_result) in zip(subscription_ids, results):
        subscription_name = subscription_names.get(subscription_id, subscription_id)
        for source, result in (
            ("reservation", reservation_result),
            ("savings_plan", savings_plan_result),
        ):
            if isinstance(result, BaseException):
                statuses.append(
                    RecommendationSourceStatus(
                        source=source,
                        subscription_id=subscription_id,
                        subscription_name=subscription_name,
                        status="unavailable",
                        message="Azure recommendation source is temporarily unavailable for this scenario.",
                    )
                )
                continue

            statuses.append(
                RecommendationSourceStatus(
                    source=source,
                    subscription_id=subscription_id,
                    subscription_name=subscription_name,
                    status="available" if result else "no_recommendation",
                    message=(
                        f"Azure returned {len(result)} recommendation(s)."
                        if result
                        else "Azure returned no recommendation for this explicit scenario."
                    ),
                )
            )
            if source == "reservation":
                reservations.extend(
                    _reservation_recommendation(item, subscription_id, subscription_name, scenario)
                    for item in result
                )
            else:
                savings_plans.extend(
                    _savings_plan_recommendation(item, subscription_id, subscription_name, scenario)
                    for item in result
                )

    sort_key = lambda item: (
        item.subscription_name.lower(),
        -(item.projected_savings or 0),
        item.sku.lower(),
    )
    return RateOptimizationResponse(
        scenario=scenario,
        generated_at=datetime.now(timezone.utc).isoformat(),
        reservations=sorted(reservations, key=sort_key),
        savings_plans=sorted(savings_plans, key=sort_key),
        source_status=statuses,
        projection_notice=(
            "Projected values are copied from Azure recommendation responses for the selected scenario. "
            "Reservation and savings-plan alternatives can overlap and are never added together."
        ),
    )
