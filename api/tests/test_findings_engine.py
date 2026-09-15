"""Mirrors the former web/src/findings/engine.test.ts (removed - the findings engine
now lives here, see findings/engine.py) - the hard rule: empty categories are
omitted, never fabricated, and all dollar arithmetic is deterministic Python, never
delegated to the LLM.
"""

from findings.engine import build_report


def test_omits_a_category_with_zero_matching_resources():
    report = build_report(["sub-1"], 1000, {"unattached_disks": [], "stopped_vms": []}, {})
    assert report.tier_a_categories == []


def test_includes_a_category_with_resources_and_computes_correct_totals():
    rows = {
        "unattached_disks": [
            {"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-1", "sizeGb": 128, "sku": "Premium_LRS"},
            {"id": "/sub/rg/disk2", "name": "disk2", "subscriptionId": "sub-1", "sizeGb": 64, "sku": "Standard_LRS"},
        ]
    }
    cost_by_resource_id = {"/sub/rg/disk1": 12.5, "/sub/rg/disk2": 4.25}
    report = build_report(
        ["sub-1"],
        1000,
        rows,
        cost_by_resource_id,
        subscription_names={"sub-1": "Subscription One"},
    )
    assert len(report.tier_a_categories) == 1
    cat = report.tier_a_categories[0]
    assert cat.category == "unattached_disks"
    assert cat.count == 2
    assert cat.monthly_total == 16.75
    assert cat.annual_total == 201
    assert {line.evidence_type for line in cat.lines} == {"verified_cost"}
    assert {line.subscription_name for line in cat.lines} == {"Subscription One"}


def test_subscription_name_falls_back_to_subscription_id():
    rows = {
        "unattached_disks": [
            {"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-unknown"},
        ]
    }

    report = build_report(["sub-unknown"], 1000, rows, {})

    assert report.tier_a_categories[0].lines[0].subscription_name == "sub-unknown"


def test_missing_cost_data_yields_null_cost_and_lower_confidence():
    rows = {
        "stopped_vms": [
            {
                "id": "/sub/rg/vm1",
                "name": "vm1",
                "subscriptionId": "sub-1",
                "vmSize": "Standard_D2s_v5",
                "powerState": "PowerState/stopped",
            }
        ]
    }
    report = build_report(["sub-1"], 1000, rows, {})
    cat = report.tier_a_categories[0]
    line = cat.lines[0]
    assert line.monthly_cost is None
    assert line.confidence == 0.5
    assert line.evidence_type == "inventory_candidate"


def test_resource_cost_evidence_is_serialized_on_matching_findings():
    evidence = {
        "/sub/rg/disk1": [
            {
                "meterId": "meter-1",
                "meterName": "Managed Disks",
                "meterCategory": "Storage",
                "meterSubCategory": "Premium SSD",
                "productId": "product-1",
                "productName": "Premium SSD Managed Disks",
                "pricingModel": "OnDemand",
                "reservationId": "",
                "reservationName": "",
                "benefitId": "",
                "benefitName": "",
                "monthlyCost": 10.0,
            }
        ]
    }
    report = build_report(
        ["sub-1"],
        100.0,
        {"unattached_disks": [{"id": "/sub/rg/disk1", "name": "disk1", "subscriptionId": "sub-1"}]},
        {"/sub/rg/disk1": 10.0},
        evidence,
    )

    serialized = report.model_dump(by_alias=True)["tierACategories"][0]["lines"][0]
    assert serialized["costEvidence"][0]["meterName"] == "Managed Disks"
    assert serialized["costEvidence"][0]["monthlyCost"] == 10.0


def test_metric_verified_network_finding_preserves_cost_but_marks_it_at_risk():
    rows = {
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
        ]
    }

    report = build_report(["sub-1"], 1000, rows, {"/sub/rg/nat1": 25.0})

    category = report.tier_a_categories[0]
    assert category.impact_type == "cost_at_risk"
    assert category.monthly_total == 25.0
    assert category.lines[0].evidence_type == "metrics_verified_idle"
    assert category.lines[0].confidence == 0.95
    assert "zero bytes and packets across 31 days" in category.lines[0].detail


def test_orphan_network_and_compute_inventory_never_inflates_savings():
    rows = {
        "unattached_network_interfaces": [
            {"id": "/sub/rg/nic1", "name": "nic1", "subscriptionId": "sub-1", "ipConfigurationCount": 1},
        ],
        "unassociated_network_security_groups": [
            {"id": "/sub/rg/nsg1", "name": "nsg1", "subscriptionId": "sub-1", "ruleCount": 3},
        ],
        "unassociated_route_tables": [
            {"id": "/sub/rg/rt1", "name": "rt1", "subscriptionId": "sub-1", "routeCount": 2},
        ],
        "empty_availability_sets": [
            {"id": "/sub/rg/av1", "name": "av1", "subscriptionId": "sub-1"},
        ],
    }
    costs = {row["id"]: 10.0 for category_rows in rows.values() for row in category_rows}

    report = build_report(["sub-1"], 1000, rows, costs)

    assert {category.impact_type for category in report.tier_a_categories} == {"inventory"}
    assert sum(category.monthly_total for category in report.tier_a_categories) == 40.0


def test_state_based_stale_categories_are_review_only_except_empty_paid_plans():
    inventory_categories = [
        "deallocated_virtual_machines",
        "zero_instance_vm_scale_sets",
        "stopped_web_apps",
        "empty_virtual_networks",
        "disconnected_private_endpoints",
        "stopped_aks_clusters",
        "empty_resource_groups",
        "old_custom_images",
    ]
    rows = {
        category: [{"id": f"/sub/rg/{category}", "name": category, "subscriptionId": "sub-1"}]
        for category in inventory_categories
    }
    rows["empty_app_service_plans"] = [
        {"id": "/sub/rg/plan1", "name": "plan1", "subscriptionId": "sub-1", "sku": "S1", "workers": 1}
    ]
    costs = {category_rows[0]["id"]: 10.0 for category_rows in rows.values()}

    report = build_report(["sub-1"], 1000, rows, costs)
    by_category = {category.category: category for category in report.tier_a_categories}

    assert {by_category[category].impact_type for category in inventory_categories} == {"inventory"}
    assert by_category["empty_app_service_plans"].impact_type == "cost_at_risk"
