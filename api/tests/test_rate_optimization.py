import asyncio

from reports.models import RateOptimizationScenario
from services import rate_optimization


def test_rate_optimization_preserves_azure_projections_and_partial_availability(monkeypatch):
    async def reservations(subscription_id, lookback, term, resource_type):
        assert (lookback, term, resource_type) == ("Last30Days", "P1Y", "VirtualMachines")
        if subscription_id == "sub-2":
            raise RuntimeError("reservation unavailable")
        return [
            {
                "id": "reservation-1",
                "location": "eastus2",
                "sku": "Standard_D4s_v5",
                "properties": {
                    "scope": "Single",
                    "resourceType": "VirtualMachines",
                    "lookBackPeriod": "Last30Days",
                    "term": "P1Y",
                    "recommendedQuantity": 2,
                    "costWithNoReservedInstances": 100,
                    "totalCostWithReservedInstances": 72,
                    "netSavings": 28,
                    "billingCurrency": "USD",
                    "firstUsageDate": "2026-07-01T00:00:00Z",
                    "lastUsageDate": "2026-07-30T00:00:00Z",
                    "totalHours": 720,
                },
            }
        ]

    async def savings_plans(subscription_id, lookback, term):
        if subscription_id == "sub-2":
            return []
        return [
            {
                "id": "savings-plan-1",
                "kind": "SavingsPlan",
                "properties": {
                    "scope": "Single",
                    "subscriptionId": subscription_id,
                    "armSkuName": "Compute_Savings_Plan",
                    "lookBackPeriod": "Last30Days",
                    "term": "P1Y",
                    "commitmentGranularity": "Hourly",
                    "costWithoutBenefit": 120,
                    "currencyCode": "USD",
                    "firstConsumptionDate": "2026-07-01T00:00:00Z",
                    "lastConsumptionDate": "2026-07-30T00:00:00Z",
                    "totalHours": 720,
                    "recommendationDetails": {
                        "commitmentAmount": 0.25,
                        "totalCost": 90,
                        "savingsAmount": 30,
                        "savingsPercentage": 25,
                        "coveragePercentage": 80,
                        "averageUtilizationPercentage": 99,
                        "wastageCost": 1.5,
                    },
                },
            }
        ]

    monkeypatch.setattr(rate_optimization.arm_client, "list_reservation_recommendations", reservations)
    monkeypatch.setattr(rate_optimization.arm_client, "list_savings_plan_recommendations", savings_plans)
    scenario = RateOptimizationScenario(
        lookBackPeriod="Last30Days",
        term="P1Y",
        reservationResourceType="VirtualMachines",
    )

    result = asyncio.run(
        rate_optimization.collect_rate_optimization(
            ["sub-1", "sub-2"], {"sub-1": "One", "sub-2": "Two"}, scenario
        )
    )

    assert result.reservations[0].projected_savings == 28
    assert result.reservations[0].savings_percentage == 0.28
    assert result.savings_plans[0].projected_savings == 30
    assert result.savings_plans[0].savings_percentage == 0.25
    assert result.savings_plans[0].coverage_percentage == 0.8
    assert result.savings_plans[0].utilization_percentage == 0.99
    assert result.reservations[0].overlap_group == result.savings_plans[0].overlap_group
    assert not hasattr(result, "projected_savings_total")
    assert [(status.source, status.subscription_id, status.status) for status in result.source_status] == [
        ("reservation", "sub-1", "available"),
        ("savings_plan", "sub-1", "available"),
        ("reservation", "sub-2", "unavailable"),
        ("savings_plan", "sub-2", "no_recommendation"),
    ]


def test_rate_optimization_keeps_valid_empty_sources_explicit(monkeypatch):
    async def empty(*args):
        return []

    monkeypatch.setattr(rate_optimization.arm_client, "list_reservation_recommendations", empty)
    monkeypatch.setattr(rate_optimization.arm_client, "list_savings_plan_recommendations", empty)
    scenario = RateOptimizationScenario(
        lookBackPeriod="Last7Days",
        term="P3Y",
        reservationResourceType="ManagedDisk",
    )

    result = asyncio.run(
        rate_optimization.collect_rate_optimization(["sub-1"], {"sub-1": "One"}, scenario)
    )

    assert result.reservations == []
    assert result.savings_plans == []
    assert all(status.status == "no_recommendation" for status in result.source_status)
