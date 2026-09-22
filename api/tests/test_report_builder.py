"""Validates the multi-tab report builder's aggregation logic (domain mapping,
subscription breakdown, Advisor reconciliation, governance) against synthetic
Resource Graph / Cost Management / Advisor data.
"""

import json

import pytest

from anomalies.models import DailyCostRecord
from reports.builder import _advisor_score, _daily_cost_trend, _tag_daily_cost_trend, build_full_report
from reports.cost_details import build_cost_details
from reports.models import FullReport
from services.focus_history_reader import FocusHistoryData
from services.focus_cost_reader import (
    FocusCommitmentBreakdown,
    FocusCostData,
    FocusPricingEvidence,
    FocusReconciliation,
    FocusResourceCost,
)


def test_cost_details_preserve_credits_tags_subcent_cost_and_verified_zero_days():
    records = [
        DailyCostRecord("2026-08-01", "sub-1", "One", "Compute", "group", "/vm", "vm", 24, tags={"app": "A", "owner": "Team"}),
        DailyCostRecord("2026-08-01", "sub-1", "One", "Compute", "group", "/vm", "vm", -2, tags={"app": "A", "owner": "Team"}),
        DailyCostRecord("2026-08-02", "sub-1", "One", "Storage", "group", "", "Unattributed", 0.0001),
    ]
    history = FocusHistoryData("USD", "2026-08-01", "2026-08-03", 3, ["2026-08"], ["sub-1"], {"sub-1": "One"}, records)
    details = build_cost_details(history)
    assert details.status == "complete"
    assert details.dates == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert sum(sum(row.daily_costs.values()) for row in details.rows) == pytest.approx(22.0001)
    assert len(details.rows) == 2
    assert next(row for row in details.rows if row.resource_id).tags == {"app": "A", "owner": "Team"}
    assert build_cost_details(history, max_rows=1).status == "unavailable"
    assert build_cost_details(history, max_rows=1).rows == []
    assert build_cost_details(None).status == "unavailable"
    assert details.model_dump(by_alias=True)["costBasis"] == "EffectiveCost"


def _sample_rows():
    return {
        "unattached_disks": [
            {"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-1", "sizeGb": 128, "sku": "Premium_LRS"},
        ],
        "stopped_vms": [
            {
                "id": "/sub/rg/vm1",
                "name": "vm1",
                "subscriptionId": "sub-1",
                "vmSize": "Standard_D2s_v5",
                "powerState": "PowerState/stopped",
            },
        ],
        "idle_public_ips": [],
        "empty_backend_pools": [],
        "old_snapshots": [
            {"id": "/sub/rg/snap1", "name": "snap1", "subscriptionId": "sub-2", "ageDays": 400, "sizeGb": 50},
        ],
    }


def test_domains_map_categories_correctly_and_omit_empty_domains_categories():
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Sub One", "sub-2": "Sub Two"},
        per_sub_spend={"sub-1": 1000.0, "sub-2": 500.0},
        resource_graph_rows=_sample_rows(),
        cost_by_resource_id={"/sub/rg/disk1": 10.0, "/sub/rg/vm1": 20.0, "/sub/rg/snap1": 5.0},
        service_family_spend={"Compute": 700.0, "Storage": 600.0, "Networking": 200.0},
        advisor_recommendations=[],
        untagged_counts={},
    )
    assert report.domains["compute"].domain_spend_month == 700.0
    assert report.domains["compute"].verified_saving_month == 20.0
    assert {c.category for c in report.domains["compute"].categories} == {"stopped_vms"}
    assert report.domains["network"].verified_saving_month == 0.0
    assert report.domains["network"].categories == []


def test_executive_summary_totals():
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Sub One", "sub-2": "Sub Two"},
        per_sub_spend={"sub-1": 1000.0, "sub-2": 500.0},
        resource_graph_rows=_sample_rows(),
        cost_by_resource_id={"/sub/rg/disk1": 10.0, "/sub/rg/vm1": 20.0, "/sub/rg/snap1": 5.0},
        service_family_spend={"Compute": 700.0, "Storage": 600.0, "Networking": 200.0},
        advisor_recommendations=[],
        untagged_counts={},
    )
    assert report.executive_summary.current_monthly_spend == 1500.0
    assert report.executive_summary.potential_savings_month == 35.0
    assert report.executive_summary.potential_savings_year == 420.0


def test_pricing_summary_is_unavailable_without_complete_reconciled_focus_data():
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        focus_unavailable_reason="No common FocusCost period.",
    )

    assert report.pricing_summary.available is False
    assert report.pricing_summary.status == "No common FocusCost period."
    assert report.pricing_summary.subscriptions == []


def test_pricing_summary_exposes_reconciled_negotiated_prices_without_inflating_savings():
    evidence = FocusPricingEvidence(
        sku_id="sku-1",
        sku_price_id="price-1",
        pricing_category="Standard",
        pricing_currency="USD",
        pricing_quantity=10,
        pricing_unit="1 Hour",
        list_unit_price=12,
        contracted_unit_price=9,
        effective_unit_price=8,
        list_cost=120,
        contracted_cost=90,
        effective_cost=80,
        billed_cost=90,
        commitment_discount_category="",
        commitment_discount_type="",
        commitment_discount_status="",
    )
    focus_data = FocusCostData(
        data_version="1.2-preview",
        period="2026-07",
        period_start="2026-07-01",
        period_end="2026-07-31",
        currency="USD",
        pricing_currencies=["USD"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Focus Sub One"},
        row_count=1,
        billed_cost_by_subscription={"sub-1": 90},
        effective_cost_by_subscription={"sub-1": 80},
        list_cost_by_subscription={"sub-1": 120},
        contracted_cost_by_subscription={"sub-1": 90},
        negotiated_discount_by_subscription={"sub-1": 30},
        billed_cost_by_resource_id={"/sub/rg/disk1": 90},
        effective_cost_by_resource_id={"/sub/rg/disk1": 80},
        service_spend={"Storage": 80},
        service_category_spend={"Storage": 80},
        pricing_evidence_by_resource_id={"/sub/rg/disk1": [evidence]},
        reservation_commitment=FocusCommitmentBreakdown(
            row_count=2,
            used_effective_cost=40,
            unused_effective_cost=5,
            realized_benefit=10,
        ),
    )
    reconciliation = FocusReconciliation(
        focus_effective_cost=80,
        amortized_cost=80,
        variance=0,
        variance_percentage=0,
        reconciled=True,
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 80.0},
        resource_graph_rows={
            "unattached_disks": [
                {"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-1"},
            ]
        },
        cost_by_resource_id={"/sub/rg/disk1": 80},
        service_family_spend={"Storage": 80},
        advisor_recommendations=[],
        untagged_counts={},
        focus_cost_data=focus_data,
        focus_reconciliation=reconciliation,
    )

    assert report.pricing_summary.available is True
    assert report.pricing_summary.negotiated_discount == 30
    assert report.pricing_summary.negotiated_discount_percentage == 0.25
    assert report.executive_summary.potential_savings_month == 80
    assert report.commitment_summary.observed is True
    assert report.commitment_summary.reservations.realized_benefit == 10
    assert report.commitment_summary.reservations.unused_effective_cost == 5
    assert report.commitment_summary.reservations.realized_benefit not in (
        report.executive_summary.potential_savings_month,
        report.executive_summary.potential_savings_year,
    )
    pricing = report.domains["storage"].categories[0].lines[0].focus_pricing_evidence[0]
    assert pricing.contracted_unit_price == 9


def test_top_services_are_ranked_from_positive_closed_period_contributors():
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        service_spend={
            "Microsoft.Storage": 250.0,
            "Microsoft.Compute": 600.0,
            "Microsoft.CognitiveServices": 100.0,
            "Credit": -25.0,
            "Microsoft.Network": 50.0,
        },
    )

    assert [item.service_name for item in report.top_services] == [
        "Microsoft.Compute",
        "Microsoft.Storage",
        "Microsoft.CognitiveServices",
        "Microsoft.Network",
    ]
    assert [item.rank for item in report.top_services] == [1, 2, 3, 4]
    assert report.top_services[0].display_name == "Virtual Machines & Compute"
    assert report.top_services[0].pct_of_total == 0.6


def test_executive_dashboard_and_storage_analysis_use_verified_dimensions():
    disk_id = "/subscriptions/sub-1/resourcegroups/rg/providers/microsoft.compute/disks/disk1"
    storage_id = "/subscriptions/sub-1/resourcegroups/rg/providers/microsoft.storage/storageaccounts/store1"
    focus_data = FocusCostData(
        data_version="1.2-preview",
        period="2026-07",
        period_start="2026-07-01",
        period_end="2026-07-31",
        currency="USD",
        pricing_currencies=["USD"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        row_count=2,
        billed_cost_by_subscription={"sub-1": 100},
        effective_cost_by_subscription={"sub-1": 100},
        list_cost_by_subscription={"sub-1": 100},
        contracted_cost_by_subscription={"sub-1": 100},
        negotiated_discount_by_subscription={"sub-1": 0},
        billed_cost_by_resource_id={disk_id: 20, storage_id: 80},
        effective_cost_by_resource_id={disk_id: 20, storage_id: 80},
        service_spend={"Storage": 100},
        service_category_spend={"Storage": 100},
        service_family_spend={"Storage": 100},
        provider_spend={"Microsoft.Storage": 80, "Microsoft.Compute": 20},
        region_spend={"eastus": 100},
        resource_costs=[
            FocusResourceCost("sub-1", "Sub One", "rg", storage_id, "store1", "Microsoft.Storage/storageAccounts", "eastus", 80),
            FocusResourceCost("sub-1", "Sub One", "rg", disk_id, "disk1", "Microsoft.Compute/disks", "eastus", 20),
        ],
    )
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-30",
        complete_days=30,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            DailyCostRecord(
                date="2026-06-01",
                subscription_id="sub-1",
                subscription_name="Sub One",
                service_name="Storage",
                resource_group="rg",
                resource_id=storage_id,
                resource_name="store1",
                effective_cost=80,
                service_category="Storage",
                resource_type="Microsoft.Storage/storageAccounts",
                region="eastus",
            )
        ],
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100},
        resource_graph_rows={
            "unattached_disks": [{"id": disk_id, "name": "disk1", "subscriptionId": "sub-1"}],
        },
        cost_by_resource_id={disk_id: 20, storage_id: 80},
        service_family_spend={"Storage": 100},
        service_spend=focus_data.provider_spend,
        advisor_recommendations=[],
        untagged_counts={},
        focus_cost_data=focus_data,
        focus_history=history,
        advisor_score_rows=[{
            "subscriptionId": "sub-1",
            "name": "AdvisorOverallScore",
            "properties": {"lastRefreshedScore": {"score": 0.82}},
        }],
        storage_accounts=[{
            "id": storage_id,
            "name": "store1",
            "subscriptionId": "sub-1",
            "resourceGroup": "rg",
            "location": "eastus",
            "currentTier": "Hot",
        }],
        storage_metrics={storage_id: {
            "complete": True,
            "capacityBytes": 3072,
            "tierBytes": {"Hot": 1024, "Cool": 2048},
            "readTransactions": 0,
            "lastAccessTrackingEnabled": True,
        }},
    )

    assert report.executive_summary.estimated_wastage_month == 20
    assert report.executive_summary.active_resources == 1
    assert report.executive_summary.idle_resources == 0
    assert report.executive_summary.idle_review_candidates == 1
    assert report.executive_summary.spend_change_percentage == 0.25
    assert report.spend_history.months[-1].category_spend == {
        "Compute": 20,
        "Storage": 80,
        "Networking": 0,
        "Databases": 0,
        "AI/ML": 0,
        "Other": 0,
    }
    assert report.cost_hierarchy[0].resource_name == "store1"
    assert report.region_spend[0].region == "eastus"
    assert report.advisor_score.score == 82
    assert report.storage_optimization.accounts[0].evidence_status == "Lifecycle candidate"
    assert report.storage_optimization.current_tier_volumes[0].tier == "Cool"
    assert report.storage_optimization.accounts[0].estimated_saving_month is None


def test_advisor_score_reads_nested_category_scores_without_recommendation_zeroes():
    rows = [
        {
            "subscriptionId": "sub-1",
            "name": "Cost",
            "properties": {"value": [
                {
                    "id": "/subscriptions/sub-1/providers/Microsoft.Advisor/advisorScore/category/Cost",
                    "name": "Cost",
                    "properties": {"latestScore": {"score": 71.34}},
                },
                {
                    "id": "/subscriptions/sub-1/providers/Microsoft.Advisor/advisorScore/category/Cost/recommendationType/example",
                    "name": "Cost/example",
                    "properties": {"latestScore": {"score": 0}},
                },
            ]},
        },
        {
            "subscriptionId": "sub-1",
            "name": "Security",
            "properties": {"value": [
                {
                    "id": "/subscriptions/sub-1/providers/Microsoft.Advisor/advisorScore/category/Security",
                    "name": "Security",
                    "properties": {"latestScore": {"score": 42.96}},
                },
                {
                    "id": "/subscriptions/sub-1/providers/Microsoft.Advisor/advisorScore/category/Security/subCategory/Example",
                    "name": "Security/Example",
                    "properties": {"latestScore": {"score": 100}},
                },
            ]},
        },
    ]

    summary = _advisor_score(rows)

    assert summary.available is True
    assert summary.score == 57.15
    assert summary.cost_score == 71.34
    assert summary.subscription_count == 1


def test_ai_usage_summary_keeps_cost_unestimated_and_builds_deterministic_opportunities():
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        ai_usage_data={
            "periodStart": "2026-08-03",
            "periodEnd": "2026-08-16",
            "eligibleAccountCount": 1,
            "deployments": [
                {
                    "accountId": "/subscriptions/sub-1/accounts/openai1",
                    "accountName": "openai1",
                    "subscriptionId": "sub-1",
                    "location": "eastus2",
                    "deploymentName": "dev-gpt4o",
                    "modelName": "gpt-4o",
                    "modelVersion": "2024-11-20",
                    "skuName": "GlobalStandard",
                    "capacity": 10,
                    "inputTokensPerDay": 800000,
                    "outputTokensPerDay": 400000,
                    "totalTokensPerDay": 1200000,
                    "estimatedCostDay": None,
                    "trendPercentage": 0.75,
                    "trendLabel": "increasing",
                    "evidenceStatus": "14-day UTC Azure Monitor token window",
                },
                {
                    "accountId": "/subscriptions/sub-1/accounts/openai1",
                    "accountName": "openai1",
                    "subscriptionId": "sub-1",
                    "location": "eastus2",
                    "deploymentName": "unused-embedding",
                    "modelName": "text-embedding-3-small",
                    "modelVersion": "1",
                    "skuName": "GlobalStandard",
                    "capacity": 10,
                    "inputTokensPerDay": 0,
                    "outputTokensPerDay": 0,
                    "totalTokensPerDay": 0,
                    "estimatedCostDay": None,
                    "trendPercentage": 0,
                    "trendLabel": "stable",
                    "evidenceStatus": "14-day UTC Azure Monitor token window",
                },
            ],
        },
    )

    assert report.ai_usage.deployments[0].deployment_name == "dev-gpt4o"
    assert report.ai_usage.deployments[0].estimated_cost_day is None
    assert {item.category for item in report.ai_usage.opportunities} == {
        "Rapid usage growth",
        "High non-production usage",
        "Idle deployment",
    }


def test_chargeback_preserves_unallocated_cost_and_reconciles_each_dimension():
    focus_data = FocusCostData(
        data_version="1.2-preview",
        period="2026-07",
        period_start="2026-07-01",
        period_end="2026-07-31",
        currency="USD",
        pricing_currencies=["USD"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        row_count=2,
        billed_cost_by_subscription={"sub-1": 100},
        effective_cost_by_subscription={"sub-1": 100},
        list_cost_by_subscription={"sub-1": 100},
        contracted_cost_by_subscription={"sub-1": 100},
        negotiated_discount_by_subscription={"sub-1": 0},
        billed_cost_by_resource_id={},
        effective_cost_by_resource_id={},
        service_spend={},
        service_category_spend={},
        tag_spend={
            "Team": {"Payments": 70, "Unallocated": 30},
            "Department": {"Engineering": 60, "Finance": 10, "Unallocated": 30},
            "Project": {"Checkout": 70, "Unallocated": 30},
        },
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        focus_cost_data=focus_data,
    )

    assert report.chargeback.allocated_cost == 70
    assert report.chargeback.unallocated_cost == 30
    assert report.chargeback.allocation_percentage == 0.7
    for dimension in ("Team", "Department", "Project"):
        assert sum(row.monthly_cost for row in report.chargeback.rows if row.dimension == dimension) == 100


def test_tag_cost_summary_excludes_unallocated_and_forecasts_from_growth_rate():
    focus_data = FocusCostData(
        data_version="1.2-preview",
        period="2026-07",
        period_start="2026-07-01",
        period_end="2026-07-31",
        currency="USD",
        pricing_currencies=["USD"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        row_count=2,
        billed_cost_by_subscription={"sub-1": 100},
        effective_cost_by_subscription={"sub-1": 100},
        list_cost_by_subscription={"sub-1": 100},
        contracted_cost_by_subscription={"sub-1": 100},
        negotiated_discount_by_subscription={"sub-1": 0},
        billed_cost_by_resource_id={},
        effective_cost_by_resource_id={},
        service_spend={},
        service_category_spend={},
        tag_spend={
            "Team": {},
            "Department": {},
            "Project": {},
            "Application": {"Checkout": 70, "Reporting": 20, "Unallocated": 10},
        },
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        focus_cost_data=focus_data,
    )

    assert report.tag_costs.available is True
    application = next(dim for dim in report.tag_costs.dimensions if dim.tag_key == "Application")
    assert application.unallocated_cost == 10
    assert {row.value for row in application.rows} == {"Checkout", "Reporting"}
    checkout = next(row for row in application.rows if row.value == "Checkout")
    assert checkout.monthly_cost == 70
    assert checkout.pct_of_total == 0.7
    # No prior month in spend history -> no growth rate -> no forecast.
    assert report.tag_costs.growth_rate is None
    assert checkout.forecast_next_month is None
    # Team/Department/Project have no values in this fixture -> no dimension emitted.
    assert {dim.tag_key for dim in report.tag_costs.dimensions} == {"Application"}


def test_extended_support_and_off_hours_savings_summaries():
    focus_data = FocusCostData(
        data_version="1.2-preview",
        period="2026-07",
        period_start="2026-07-01",
        period_end="2026-07-31",
        currency="USD",
        pricing_currencies=["USD"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        row_count=3,
        billed_cost_by_subscription={"sub-1": 300},
        effective_cost_by_subscription={"sub-1": 300},
        list_cost_by_subscription={"sub-1": 300},
        contracted_cost_by_subscription={"sub-1": 300},
        negotiated_discount_by_subscription={"sub-1": 0},
        billed_cost_by_resource_id={},
        effective_cost_by_resource_id={},
        service_spend={},
        service_category_spend={},
        resource_costs=[
            FocusResourceCost(
                subscription_id="sub-1",
                subscription_name="Sub One",
                resource_group="rg-1",
                resource_id="/subscriptions/sub-1/resourcegroups/rg-1/providers/microsoft.compute/virtualmachines/vm-esu",
                resource_name="vm-esu",
                resource_type="Microsoft.Compute/virtualMachines",
                region="eastus",
                effective_cost=200.0,
            ),
            FocusResourceCost(
                subscription_id="sub-1",
                subscription_name="Sub One",
                resource_group="rg-1",
                resource_id="/subscriptions/sub-1/resourcegroups/rg-1/providers/microsoft.storage/storageaccounts/st-1",
                resource_name="st-1",
                resource_type="Microsoft.Storage/storageAccounts",
                region="eastus",
                effective_cost=100.0,
            ),
        ],
        extended_support_costs=[
            FocusResourceCost(
                subscription_id="sub-1",
                subscription_name="Sub One",
                resource_group="rg-1",
                resource_id="/subscriptions/sub-1/resourcegroups/rg-1/providers/microsoft.compute/virtualmachines/vm-esu",
                resource_name="vm-esu",
                resource_type="Microsoft.Compute/virtualMachines",
                region="eastus",
                effective_cost=50.0,
            ),
        ],
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 300},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        focus_cost_data=focus_data,
    )

    assert report.extended_support.available is True
    assert report.extended_support.total_monthly_cost == 50.0
    assert report.extended_support.rows[0].resource_name == "vm-esu"

    # Only the VM is eligible for off-hours savings, not the storage account.
    assert report.off_hours_savings.available is True
    assert len(report.off_hours_savings.rows) == 1
    saving_row = report.off_hours_savings.rows[0]
    assert saving_row.resource_name == "vm-esu"
    assert saving_row.monthly_cost == 200.0
    assert saving_row.estimated_monthly_saving == round(200.0 * report.off_hours_savings.off_hours_fraction, 2)


def test_compliance_keeps_policy_evaluations_distinct_from_resource_tagging():
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Sub One", "sub-2": "Sub Two"},
        per_sub_spend={"sub-1": 50, "sub-2": 50},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={
            "sub-1": {"total": 10, "untagged": 2},
            "sub-2": {"total": 5, "untagged": 5},
        },
        policy_compliance_rows=[{
            "subscriptionId": "sub-1",
            "compliantEvaluations": 80,
            "nonCompliantEvaluations": 15,
            "conflictEvaluations": 5,
            "exemptEvaluations": 3,
            "notStartedEvaluations": 2,
            "nonCompliantResources": 4,
            "policyAssignmentCount": 7,
        }],
    )

    by_id = {row.subscription_id: row for row in report.compliance.rows}
    assert by_id["sub-1"].tagging_percentage == 0.8
    assert by_id["sub-1"].evaluation_compliance_percentage == 0.8
    assert by_id["sub-1"].non_compliant_resources == 4
    assert by_id["sub-1"].non_compliant_evaluations == 15
    assert by_id["sub-2"].policy_data_available is False
    assert by_id["sub-2"].tagging_percentage == 0


def test_roadmap_prioritizes_immediate_actions_before_high_value_review_items():
    rows = {
        "unattached_disks": [
            {
                "id": "/sub/rg/disk1",
                "name": "disk1",
                "subscriptionId": "sub-1",
                "sizeGb": 128,
                "sku": "Premium_LRS",
            }
        ],
        "empty_backend_pools": [
            {
                "id": "/sub/rg/gateway1",
                "name": "gateway1",
                "subscriptionId": "sub-1",
            }
        ],
    }
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows=rows,
        cost_by_resource_id={"/sub/rg/disk1": 10.0, "/sub/rg/gateway1": 200.0},
        service_family_spend={"Storage": 100.0, "Networking": 400.0},
        advisor_recommendations=[],
        untagged_counts={},
    )

    assert [item.category for item in report.savings_roadmap] == [
        "unattached_disks",
        "empty_backend_pools",
    ]
    assert report.savings_roadmap[0].immediate is True
    assert report.savings_roadmap[1].immediate is False
    assert report.savings_roadmap[0].impact_type == "potential_savings"
    assert report.savings_roadmap[1].impact_type == "cost_at_risk"
    assert report.savings_roadmap[0].affected_subscriptions[0].subscription_name == "Sub One"
    assert report.action_plan[0].affected_subscriptions[0].subscription_name == "Sub One"
    assert report.savings_roadmap[1].recommended_action.startswith("Inspect routing dependencies")
    assert all("Application Gateways" not in item.action for item in report.action_plan)
    assert report.domains["storage"].categories[0].remediation.mode == "preview"
    assert report.domains["network"].categories[0].remediation.mode == "inspect"
    assert report.domains["network"].verified_saving_month == 0.0
    assert report.domains["network"].monthly_cost_at_risk == 200.0
    assert report.executive_summary.potential_savings_month == 10.0


def test_subscription_breakdown_attributes_cost_to_the_right_subscription():
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Sub One", "sub-2": "Sub Two"},
        per_sub_spend={"sub-1": 1000.0, "sub-2": 500.0},
        resource_graph_rows=_sample_rows(),
        cost_by_resource_id={"/sub/rg/disk1": 10.0, "/sub/rg/vm1": 20.0, "/sub/rg/snap1": 5.0},
        service_family_spend={"Compute": 700.0, "Storage": 600.0, "Networking": 200.0},
        advisor_recommendations=[],
        untagged_counts={},
    )
    by_id = {row.subscription_id: row for row in report.subscription_breakdown}
    assert by_id["sub-1"].total_waste == 30.0
    assert by_id["sub-2"].total_waste == 5.0
    assert by_id["sub-1"].category_costs["old_snapshots"] == 0.0
    assert by_id["sub-2"].category_costs["old_snapshots"] == 5.0


def test_aggregate_actions_list_all_affected_subscriptions_by_name():
    rows = {
        "unattached_disks": [
            {"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-1"},
            {"id": "/sub/rg/disk2", "name": "disk2", "subscriptionId": "sub-2"},
        ]
    }
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Zulu Subscription", "sub-2": "Alpha Subscription"},
        per_sub_spend={"sub-1": 100.0, "sub-2": 100.0},
        resource_graph_rows=rows,
        cost_by_resource_id={"/sub/rg/disk1": 10.0, "/sub/rg/disk2": 20.0},
        service_family_spend={"Storage": 200.0},
        advisor_recommendations=[],
        untagged_counts={},
    )

    expected = ["Alpha Subscription", "Zulu Subscription"]
    assert [item.subscription_name for item in report.savings_roadmap[0].affected_subscriptions] == expected
    assert [item.subscription_name for item in report.action_plan[0].affected_subscriptions] == expected


def test_network_expansion_reports_billed_cost_at_risk_without_inflating_savings():
    rows = {
        "empty_load_balancer_backend_pools": [
            {
                "id": "/sub/rg/lb1",
                "name": "lb1",
                "subscriptionId": "sub-1",
                "sku": "Standard",
                "backendPoolCount": 1,
            }
        ],
        "idle_nat_gateways": [
            {
                "id": "/sub/rg/nat1",
                "name": "nat1",
                "subscriptionId": "sub-1",
                "sku": "Standard",
                "subnetCount": 2,
                "metricDays": 31,
                "evidenceType": "metrics_verified_idle",
                "confidence": 0.95,
            }
        ],
    }
    coverage = [
        {
            "category": "idle_nat_gateways",
            "candidateCount": 2,
            "completeCount": 1,
            "zeroTrafficCount": 1,
            "unavailableCount": 1,
            "periodStart": "2026-07-01",
            "periodEnd": "2026-07-31",
        }
    ]
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows=rows,
        cost_by_resource_id={"/sub/rg/lb1": 15.0, "/sub/rg/nat1": 25.0},
        service_family_spend={"Networking": 400.0},
        advisor_recommendations=[],
        untagged_counts={},
        network_metric_coverage=coverage,
    )

    assert report.executive_summary.potential_savings_month == 0.0
    assert report.domains["network"].verified_saving_month == 0.0
    assert report.domains["network"].monthly_cost_at_risk == 40.0
    assert {item.impact_type for item in report.savings_roadmap} == {"cost_at_risk"}
    assert report.action_plan == []
    assert report.network_metric_coverage[0].unavailable_count == 1
    assert report.network_metric_coverage[0].zero_traffic_count == 1


def test_sql_ahb_and_ai_inventory_do_not_inflate_savings_or_cost_at_risk():
    rows = {
        "sql_databases_and_pools": [
            {"id": "/sub/providers/microsoft.sql/servers/sql1", "name": "sql1", "subscriptionId": "sub-1", "resourceKind": "SQL logical server"}
        ],
        "compute_ahb_candidates": [
            {"id": "/sub/providers/microsoft.compute/virtualmachines/vm1", "name": "vm1", "subscriptionId": "sub-1", "offer": "WindowsServer", "imageSku": "2022-datacenter", "licenseType": "", "vmSize": "Standard_D2s_v3"}
        ],
        "ai_cognitive_accounts": [
            {"id": "/sub/providers/microsoft.cognitiveservices/accounts/ai1", "name": "ai1", "subscriptionId": "sub-1", "resourceKind": "Cognitive Services account", "accountKind": "AIServices", "skuName": "S0"}
        ],
        "ai_foundry_projects": [
            {"id": "/sub/providers/microsoft.cognitiveservices/accounts/ai1/projects/project1", "name": "ai1/project1", "subscriptionId": "sub-1", "parentAccountId": "/sub/providers/microsoft.cognitiveservices/accounts/ai1", "suppressCost": True}
        ],
    }
    costs = {
        "/sub/providers/microsoft.sql/servers/sql1": 40.0,
        "/sub/providers/microsoft.compute/virtualmachines/vm1": 100.0,
        "/sub/providers/microsoft.cognitiveservices/accounts/ai1": 25.0,
        "/sub/providers/microsoft.cognitiveservices/accounts/ai1/projects/project1": 25.0,
    }
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows=rows,
        cost_by_resource_id=costs,
        service_family_spend={"Compute": 500.0},
        service_spend={"Microsoft.Sql": 40.0, "Microsoft.CognitiveServices": 25.0},
        advisor_recommendations=[],
        untagged_counts={},
    )

    assert report.executive_summary.potential_savings_month == 0.0
    assert report.savings_roadmap == []
    assert report.action_plan == []
    assert report.domains["sql"].domain_spend_month == 40.0
    assert report.domains["ai"].domain_spend_month == 25.0
    assert report.domains["compute"].monthly_cost_at_risk == 0.0
    assert report.domains["compute"].categories[0].impact_type == "inventory"
    project = report.domains["ai"].categories[1].lines[0]
    assert project.monthly_cost is None
    assert project.cost_evidence == []


def test_sql_and_ai_advisor_guidance_is_matched_by_resource_provider():
    advisor = [
        {
            "properties": {
                "resourceMetadata": {"resourceId": "/sub/providers/Microsoft.Sql/servers/sql1/databases/db1"},
                "shortDescription": {"problem": "SQL purchasing model", "solution": "Review reservation coverage"},
                "extendedProperties": {"annualSavingsAmount": "120"},
            }
        },
        {
            "properties": {
                "resourceMetadata": {"resourceId": "/sub/providers/Microsoft.CognitiveServices/accounts/ai1"},
                "shortDescription": {"problem": "AI service configuration", "solution": "Review the account"},
                "extendedProperties": {"savingsAmount": "15", "savingsPeriod": "monthly"},
            }
        },
    ]
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows={},
        cost_by_resource_id={
            "/sub/providers/microsoft.sql/servers/sql1/databases/db1": 20.0,
            "/sub/providers/microsoft.cognitiveservices/accounts/ai1": 10.0,
        },
        service_family_spend={},
        service_spend={},
        advisor_recommendations=advisor,
        untagged_counts={},
    )

    sql_advisor = report.domains["sql"].advisor_recommendations[0]
    ai_advisor = report.domains["ai"].advisor_recommendations[0]
    assert sql_advisor.estimated_savings == 120.0
    assert sql_advisor.subscription_id == "sub-1"
    assert sql_advisor.subscription_name == "Sub One"
    assert sql_advisor.savings_period == "annual"
    assert sql_advisor.billed_cost == 20.0
    assert ai_advisor.estimated_savings == 15.0
    assert ai_advisor.savings_period == "monthly"
    assert ai_advisor.billed_cost == 10.0


def test_advisor_reconciliation_flags_resources_missing_from_billing():
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 1000.0},
        resource_graph_rows={"unattached_disks": [], "stopped_vms": [], "idle_public_ips": [], "empty_backend_pools": [], "old_snapshots": []},
        cost_by_resource_id={"/sub/rg/vmbilled": 42.0},
        service_family_spend={},
        advisor_recommendations=[
            {
                "properties": {
                    "category": "Cost",
                    "resourceMetadata": {"resourceId": "/sub/rg/vmbilled"},
                    "extendedProperties": {"annualSavingsAmount": "100"},
                }
            },
            {
                "properties": {
                    "category": "Cost",
                    "resourceMetadata": {"resourceId": "/sub/rg/vmnotbilled"},
                    "extendedProperties": {"savingsAmount": "50"},
                }
            },
        ],
        untagged_counts={},
    )
    measures = {m.measure: m.value for m in report.advisor_reconciliation.measures}
    assert measures["Advisor cost recommendations in scope"] == "2"
    assert measures["Flagged resources with no billing record"] == "1 of 2"
    monetary_values = {
        measure.measure: measure.monetary_value
        for measure in report.advisor_reconciliation.measures
    }
    assert monetary_values["Advisor claimed saving (as reported)"] == 150.0
    assert monetary_values["Closed-period effective cost of flagged resources"] == 42.0
    assert [item.subscription_name for item in report.advisor_reconciliation.recommendations] == ["Sub One", "Sub One"]


def test_governance_computes_pct_of_estate():
    report = build_full_report(
        subscription_ids=["sub-1", "sub-2"],
        subscription_names={"sub-1": "Sub One", "sub-2": "Sub Two"},
        per_sub_spend={"sub-1": 1000.0, "sub-2": 500.0},
        resource_graph_rows={"unattached_disks": [], "stopped_vms": [], "idle_public_ips": [], "empty_backend_pools": [], "old_snapshots": []},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={
            "sub-1": {"total": 100, "untagged": 30},
            "sub-2": {"total": 100, "untagged": 10},
        },
    )
    by_id = {row.subscription_id: row for row in report.governance}
    assert by_id["sub-1"].pct_of_estate == 0.75
    assert by_id["sub-2"].pct_of_estate == 0.25


def _commitment_history_record(**overrides) -> DailyCostRecord:
    defaults = dict(
        date="2026-06-01",
        subscription_id="sub-1",
        subscription_name="Sub One",
        service_name="Virtual Machines",
        resource_group="rg",
        resource_id="/sub/rg/vm1",
        resource_name="vm1",
        effective_cost=0.0,
        service_category="Compute",
        resource_type="Microsoft.Compute/virtualMachines",
        region="eastus",
        charge_category="Usage",
        list_cost=0.0,
        contracted_cost=0.0,
        commitment_discount_type="",
        commitment_discount_status="",
        pricing_category="Standard",
    )
    defaults.update(overrides)
    return DailyCostRecord(**defaults)


def test_commitment_insights_computes_mom_reservation_savings_plan_spot_and_coverage():
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-07-01",
        complete_days=61,
        periods=["2026-06", "2026-07"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            # June: reservation-used VM (covers item 1, 4, 6)
            _commitment_history_record(
                date="2026-06-01",
                resource_id="/sub/rg/vm-ri",
                effective_cost=40.0,
                contracted_cost=50.0,
                list_cost=70.0,
                commitment_discount_type="Reservation",
                commitment_discount_status="Used",
            ),
            # June: reservation-unused (non-VM, still counts toward committed cost)
            _commitment_history_record(
                date="2026-06-01",
                resource_id="/sub/rg/sql-ri",
                resource_type="Microsoft.Sql/servers",
                service_category="Databases",
                effective_cost=5.0,
                commitment_discount_type="Reservation",
                commitment_discount_status="Unused",
            ),
            # June: savings-plan-used VM (covers item 2, 3, 7)
            _commitment_history_record(
                date="2026-06-01",
                resource_id="/sub/rg/vm-sp",
                effective_cost=30.0,
                contracted_cost=36.0,
                list_cost=50.0,
                commitment_discount_type="SavingsPlan",
                commitment_discount_status="Used",
            ),
            # June: PAYG VM (uncovered, same resource type as the reservation for coverage %)
            _commitment_history_record(
                date="2026-06-01",
                resource_id="/sub/rg/vm-payg",
                effective_cost=20.0,
                list_cost=20.0,
            ),
            # June: Spot VM (covers item 8; also PAYG for coverage purposes)
            _commitment_history_record(
                date="2026-06-01",
                resource_id="/sub/rg/vm-spot",
                effective_cost=8.0,
                list_cost=20.0,
                pricing_category="Spot",
            ),
            # July: only a Spot row, no commitment activity at all this month
            _commitment_history_record(
                date="2026-07-01",
                resource_id="/sub/rg/vm-spot-2",
                effective_cost=5.0,
                list_cost=10.0,
                pricing_category="Spot",
            ),
        ],
    )
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
        focus_history=history,
    )

    insights = report.commitment_insights
    assert insights.available is True
    assert [point.month for point in insights.months] == ["2026-06", "2026-07"]

    june = insights.months[0]
    # Item 1 & 6: RI committed cost and realized savings
    assert june.reservation_committed_cost == 45.0
    assert june.reservation_used_cost == 40.0
    assert june.reservation_unused_cost == 5.0
    assert june.reservation_realized_savings == 10.0
    # Item 4: org-level reservation coverage = RI used / (RI used + eligible PAYG,
    # where eligible PAYG includes the uncovered PAYG VM and the Spot VM of the same type)
    assert june.reservation_coverage_percentage == pytest.approx(40.0 / 68.0)
    # Item 2 & 7: Savings Plan committed cost and realized savings
    assert june.savings_plan_committed_cost == 30.0
    assert june.savings_plan_realized_savings == 6.0
    # Item 5: ACD (RI+SP used) vs least price
    assert june.acd_effective_cost == 70.0
    assert june.acd_least_price_cost == 120.0
    assert june.acd_savings == 50.0
    assert june.acd_savings_percentage == pytest.approx(50.0 / 120.0)
    # Item 8: Spot savings
    assert june.spot_effective_cost == 8.0
    assert june.spot_least_price_cost == 20.0
    assert june.spot_savings == 12.0
    # Item 3: Compute VM PAYG vs Savings Plan vs Reservation coverage
    coverage = june.compute_vm_coverage
    assert coverage.total_cost == 98.0
    assert coverage.reservation_cost == 40.0
    assert coverage.savings_plan_cost == 30.0
    assert coverage.payg_cost == 28.0
    assert coverage.payg_percentage == pytest.approx(28.0 / 98.0)

    july = insights.months[1]
    assert july.reservation_committed_cost == 0.0
    assert july.reservation_coverage_percentage is None
    assert july.acd_savings_percentage is None
    assert july.spot_savings == 5.0


def test_daily_cost_trend_aggregates_by_exact_date_and_derives_average_hourly_cost():
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-02",
        complete_days=2,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm1", effective_cost=24.0),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm2", effective_cost=24.0),
            _commitment_history_record(date="2026-06-02", resource_id="/sub/rg/vm1", effective_cost=48.0),
        ],
    )

    trend = _daily_cost_trend(history)

    assert [point.date for point in trend.days] == ["2026-06-01", "2026-06-02"]
    assert trend.days[0].total_cost == 48.0
    assert trend.days[0].average_hourly_cost == 2.0
    assert trend.days[1].total_cost == 48.0
    assert trend.days[1].average_hourly_cost == 2.0


def test_daily_cost_trend_is_unavailable_without_focus_history():
    trend = _daily_cost_trend(None)

    assert trend.status == "unavailable"
    assert trend.days == []


def test_daily_calendar_includes_zero_cost_days_inside_verified_history_and_keeps_small_hourly_cost():
    history = FocusHistoryData(currency="USD", history_start="2026-06-01", history_end="2026-06-10",
        complete_days=10, periods=["2026-06"], subscription_ids=["sub-1"], subscription_names={}, records=[
            _commitment_history_record(date="2026-06-01", effective_cost=100),
            _commitment_history_record(date="2026-06-10", effective_cost=0.01),
        ])
    trend = _daily_cost_trend(history, days=7)
    assert len(trend.days) == 7
    assert trend.days[0].date == "2026-06-04"
    assert trend.days[0].total_cost == 0
    assert trend.days[-1].average_hourly_cost > 0
    assert sum(point.total_cost for point in trend.days) == 0.01


def test_full_report_loads_from_json_missing_daily_cost_trend_fields():
    # Regression: snapshots saved before dailyCostTrend/tagDailyCostTrend existed
    # must still parse (services/report_snapshots.py's model_validate_json call),
    # not raise and surface as "Completed report snapshot is unavailable".
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
    )
    payload = json.loads(report.model_dump_json(by_alias=True))
    del payload["dailyCostTrend"]
    del payload["tagDailyCostTrend"]

    reloaded = FullReport.model_validate_json(json.dumps(payload))

    assert reloaded.daily_cost_trend.status == "unavailable"
    assert reloaded.tag_daily_cost_trend.status == "unavailable"


def test_tag_daily_cost_trend_ranks_by_total_spend_and_derives_average_hourly_cost():
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-02",
        complete_days=2,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm-big", effective_cost=96.0, tags={"application": "checkout"}),
            _commitment_history_record(date="2026-06-02", resource_id="/sub/rg/vm-big", effective_cost=48.0, tags={"application": "checkout"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm-small", effective_cost=1.0, tags={"application": "reporting"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm-untagged", effective_cost=500.0, tags={}),
        ],
    )

    trend = _tag_daily_cost_trend(history, top_n=1)

    assert trend.tag_key == "Application"
    assert trend.available_tag_values == ["Application: checkout"]
    assert len(trend.series) == 1
    assert trend.series[0].tag_key == "Application"
    assert trend.series[0].tag_value == "Application: checkout"
    assert trend.series[0].total_cost == 144.0
    assert [point.date for point in trend.series[0].days] == ["2026-06-01", "2026-06-02"]
    assert trend.series[0].days[0].total_cost == 96.0
    assert trend.series[0].days[0].average_hourly_cost == 4.0
    assert trend.status == "partial"


def test_tag_daily_cost_trend_is_unavailable_without_focus_history():
    trend = _tag_daily_cost_trend(None)

    assert trend.status == "unavailable"
    assert trend.series == []


def test_tag_sets_do_not_double_count_and_use_one_calendar_window():
    history = FocusHistoryData(currency="USD", history_start="2026-06-01", history_end="2026-06-10",
        complete_days=10, periods=["2026-06"], subscription_ids=["sub-1"], subscription_names={}, records=[
            _commitment_history_record(date="2026-06-01", effective_cost=1000, tags={"team": "old"}),
            _commitment_history_record(date="2026-06-10", effective_cost=24, tags={"team": "A", "app": "B"}),
            _commitment_history_record(date="2026-06-10", effective_cost=12, tags={}),
            _commitment_history_record(date="2026-06-09", effective_cost=-6, tags={"team": "credit"}),
        ])
    trend = _tag_daily_cost_trend(history, days=7)
    assert len(trend.window_dates) == 7
    assert trend.window_dates[0] == "2026-06-04"
    assert sum(item.total_cost for item in trend.distribution_series) == 30
    assert any(item.tag_value == "App: B; Team: A" for item in trend.distribution_series)
    assert any(item.tag_value == "Untagged" for item in trend.distribution_series)
    assert "Team: old" not in trend.available_tag_values
    for item in [*trend.series, *trend.distribution_series]:
        assert len(item.days) == 7
        assert item.total_cost == pytest.approx(sum(day.total_cost for day in item.days))


def test_tag_set_overflow_is_retained_in_other_bucket():
    history = FocusHistoryData(currency="USD", history_start="2026-06-01", history_end="2026-06-01",
        complete_days=1, periods=["2026-06"], subscription_ids=["sub-1"], subscription_names={}, records=[
            _commitment_history_record(date="2026-06-01", effective_cost=24, tags={"team": str(index)}) for index in range(15)
        ])
    trend = _tag_daily_cost_trend(history)
    assert len(trend.distribution_series) == 11
    assert sum(item.total_cost for item in trend.distribution_series) == 360
    assert trend.distribution_series[-1].tag_value == "Other tag sets"


def test_tag_daily_cost_trend_works_when_tenant_has_no_application_tag():
    # Regression: this tenant might not literally tag anything "application" - the
    # feature must still light up using whichever tag key is actually populated.
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-01",
        complete_days=1,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm1", effective_cost=48.0, tags={"project": "checkout"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm2", effective_cost=24.0, tags={"project": "reporting"}),
        ],
    )

    trend = _tag_daily_cost_trend(history)

    assert trend.tag_key == "Project"
    assert trend.status == "complete"
    assert trend.available_tag_values == ["Project: checkout", "Project: reporting"]


def test_tag_daily_cost_trend_includes_every_tag_key_not_just_one():
    # Regression: grouping by a single auto-detected key hid every other tag key a
    # tenant actually uses. All populated keys must appear together in one ranking,
    # not just whichever one the resolver used to prefer.
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-01",
        complete_days=1,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm1", effective_cost=500.0, tags={"workload": "sre-agent-demo"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm2", effective_cost=10.0, tags={"team": "checkout"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm3", effective_cost=10.0, tags={"team": "reporting"}),
            _commitment_history_record(date="2026-06-01", resource_id="/sub/rg/vm4", effective_cost=10.0, tags={"team": "billing"}),
        ],
    )

    trend = _tag_daily_cost_trend(history)

    assert trend.tag_key == "Team, Workload"
    assert trend.available_tag_values == [
        "Workload: sre-agent-demo",
        "Team: checkout",
        "Team: reporting",
        "Team: billing",
    ]


def test_tag_daily_cost_trend_ignores_system_timestamp_tags():
    # Regression: Azure auto-injects properties like "virtualMachineProfileTimeCreated"
    # into the same Tags blob as real tags on VMSS instances. Every value is a unique
    # timestamp, so it would win on richness alone despite being meaningless for cost
    # grouping - it must be excluded in favor of the real, repeating "team" tag.
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-01",
        complete_days=1,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm1", effective_cost=100.0,
                tags={"virtualmachineprofiletimecreated": "6/30/2026 4:00:16 AM +00:00", "team": "checkout"},
            ),
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm2", effective_cost=100.0,
                tags={"virtualmachineprofiletimecreated": "7/23/2026 4:11:29 AM +00:00", "team": "reporting"},
            ),
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm3", effective_cost=100.0,
                tags={"virtualmachineprofiletimecreated": "6/24/2026 8:34:58 AM +00:00", "team": "checkout"},
            ),
        ],
    )

    trend = _tag_daily_cost_trend(history)

    assert trend.tag_key == "Team"
    assert trend.available_tag_values == ["Team: checkout", "Team: reporting"]


def test_tag_daily_cost_trend_ignores_aks_managed_platform_tags():
    # Regression: AKS auto-tags every resource in a node resource group with keys
    # like "aks-managed-cluster-name". One value per cluster gives it lots of
    # distinct values, which would beat a real, repeating "team" tag on richness
    # alone - it must be excluded as platform-managed, not a business cost category.
    history = FocusHistoryData(
        currency="USD",
        history_start="2026-06-01",
        history_end="2026-06-01",
        complete_days=1,
        periods=["2026-06"],
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        records=[
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm1", effective_cost=100.0,
                tags={"aks-managed-cluster-name": "sre-demo-aks", "team": "checkout"},
            ),
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm2", effective_cost=100.0,
                tags={"aks-managed-cluster-name": "sre-aks", "team": "reporting"},
            ),
            _commitment_history_record(
                date="2026-06-01", resource_id="/sub/rg/vm3", effective_cost=100.0,
                tags={"aks-managed-cluster-name": "cluster1", "team": "checkout"},
            ),
        ],
    )

    trend = _tag_daily_cost_trend(history)

    assert trend.tag_key == "Team"
    assert trend.available_tag_values == ["Team: checkout", "Team: reporting"]


def test_commitment_insights_is_unavailable_without_focus_history():
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
    )

    assert report.commitment_insights.available is False
    assert report.commitment_insights.months == []
