import pytest
from pathlib import Path

from findings.engine import build_report
from findings.models import AssessmentReport, SqlResourceContext
from findings.sql_inventory import SQL_INVENTORY_TYPES, classify_sql_resource

_RESOURCE_PREFIX = "/subscriptions/sub-1/resourceGroups/rg/providers"


@pytest.mark.parametrize("metadata, expected_model", [
    ({"type": "microsoft.sql/servers/databases", "elasticPoolId": ""}, "single_database"),
    ({"type": "Microsoft.Sql/servers/databases", "elasticPoolId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Sql/servers/server/elasticPools/pool"}, "pooled_database"),
    ({"type": "microsoft.sql/servers/databases"}, "unknown"),
    ({"resourceKind": "SQL Database"}, "unknown"),
    ({"type": "microsoft.synapse/workspaces/sqlpools"}, "unknown"),
])
def test_sql_report_preserves_deployment_model_without_guessing(metadata, expected_model):
    row = {
        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Sql/servers/server/databases/app",
        "name": "app",
        "subscriptionId": "sub-1",
        **metadata,
    }
    report = build_report(["sub-1"], 0, {"sql_databases_and_pools": [row]}, {})
    line = report.tier_a_categories[0].lines[0]

    assert line.sql_context is not None
    assert line.sql_context.deployment_model == expected_model
    assert report.tier_a_categories[0].impact_type == "inventory"
    assert line.monthly_cost is None
    assert line.evidence_type == "inventory_candidate"


@pytest.mark.parametrize("category, resource_type, expected_model, metadata", [
    ("sql_databases_and_pools", "microsoft.sql/servers", "logical_server", {}),
    ("sql_databases_and_pools", "microsoft.sql/servers/elasticpools", "elastic_pool", {}),
    ("sql_managed_instances_and_pools", "microsoft.sql/managedinstances", "managed_instance", {"instancePoolId": ""}),
    ("sql_managed_instances_and_pools", "microsoft.sql/instancepools", "instance_pool", {}),
    ("sql_virtual_machines", "microsoft.sqlvirtualmachine/sqlvirtualmachines", "sql_vm", {"billingResourceId": f"{_RESOURCE_PREFIX}/Microsoft.Compute/virtualMachines/vm"}),
])
def test_existing_sql_models_are_distinct(category, resource_type, expected_model, metadata):
    context = classify_sql_resource(category, {"type": resource_type, "subscriptionId": "sub-1", **metadata})
    assert context is not None
    assert context.deployment_model == expected_model
    assert context.evidence_gaps == []
    assert context.schema_version == "1.0"


@pytest.mark.parametrize("pool_id", [
    "pool-name-only",
    f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/other/elasticPools/pool",
    f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server/databases/pool",
    "/subscriptions/other/resourceGroups/rg/providers/Microsoft.Sql/servers/server/elasticPools/pool",
    None,
    {},
])
def test_invalid_or_missing_pool_relationship_never_implies_standalone(pool_id):
    context = classify_sql_resource("sql_databases_and_pools", {
        "type": "microsoft.sql/servers/databases",
        "id": f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server/databases/app",
        "subscriptionId": "sub-1",
        "elasticPoolId": pool_id,
    })
    assert context.deployment_model == "unknown"
    assert context.pool_resource_id is None
    assert context.evidence_gaps


def test_mi_pool_and_sql_vm_relationships_are_preserved():
    instance_pool_id = f"{_RESOURCE_PREFIX}/Microsoft.Sql/instancePools/pool"
    instance = classify_sql_resource("sql_managed_instances_and_pools", {
        "type": "microsoft.sql/managedinstances",
        "instancePoolId": instance_pool_id,
        "subscriptionId": "sub-1",
    })
    assert instance.deployment_model == "managed_instance"
    assert instance.pool_resource_id == instance_pool_id
    assert instance.evidence_gaps == []

    compute_id = f"{_RESOURCE_PREFIX}/Microsoft.Compute/virtualMachines/vm"
    virtual_machine = classify_sql_resource("sql_virtual_machines", {
        "type": "microsoft.sqlvirtualmachine/sqlvirtualmachines",
        "billingResourceId": compute_id,
        "subscriptionId": "sub-1",
    })
    assert virtual_machine.compute_resource_id == compute_id
    assert virtual_machine.evidence_gaps == []


@pytest.mark.parametrize("reference", [None, "vm-name", {}, f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server"])
def test_sql_vm_requires_valid_compute_reference(reference):
    context = classify_sql_resource("sql_virtual_machines", {
        "type": "microsoft.sqlvirtualmachine/sqlvirtualmachines",
        "billingResourceId": reference,
        "subscriptionId": "sub-1",
    })
    assert context.deployment_model == "sql_vm"
    assert context.compute_resource_id is None
    assert context.evidence_gaps == ["compute_resource_id_missing_or_invalid"]


@pytest.mark.parametrize("name, resource_name", [("master", "master"), ("server/MASTER", "MASTER"), ("unexpected", "master")])
def test_system_database_rows_are_excluded_from_sql_findings(name, resource_name):
    row = {
        "id": f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server/databases/{resource_name}",
        "name": name,
        "type": "Microsoft.Sql/servers/databases",
        "subscriptionId": "sub-1",
        "elasticPoolId": "",
    }
    report = build_report(["sub-1"], 0, {"sql_databases_and_pools": [row]}, {})
    assert report.tier_a_categories == []


def test_logical_server_named_master_is_not_a_system_database():
    row = {"id": f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/master", "name": "master", "type": "Microsoft.Sql/servers"}
    report = build_report(["sub-1"], 0, {"sql_databases_and_pools": [row]}, {})
    assert report.tier_a_categories[0].lines[0].sql_context.deployment_model == "logical_server"


@pytest.mark.parametrize("resource_type, sku_tier", [
    ("microsoft.sql/servers/databases", "DataWarehouse"),
    ("microsoft.synapse/workspaces/sqlpools", ""),
    ("microsoft.fabric/capacities", ""),
])
def test_dedicated_pool_and_fabric_are_not_silently_classified_as_single_databases(resource_type, sku_tier):
    context = classify_sql_resource("sql_databases_and_pools", {
        "type": resource_type, "skuTier": sku_tier, "elasticPoolId": "",
    })
    assert context.deployment_model == "unknown"
    assert context.evidence_gaps


def test_non_sql_findings_have_no_sql_context():
    assert classify_sql_resource("unattached_disks", {"type": "microsoft.sql/servers/databases"}) is None


def test_sql_context_round_trips_and_legacy_reports_default_to_unclassified():
    report = build_report(["sub-1"], 0, {"sql_databases_and_pools": [{
        "id": f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server/databases/app",
        "type": "microsoft.sql/servers/databases",
        "elasticPoolId": "",
    }]}, {})
    payload = report.model_dump(by_alias=True)
    context = payload["tierACategories"][0]["lines"][0]["sqlContext"]
    assert context["deploymentModel"] == "single_database"
    assert context["schemaVersion"] == "1.0"
    assert AssessmentReport.model_validate_json(report.model_dump_json(by_alias=True)) == report
    del payload["tierACategories"][0]["lines"][0]["sqlContext"]
    legacy = AssessmentReport.model_validate(payload)
    assert legacy.tier_a_categories[0].lines[0].sql_context is None
    assert SqlResourceContext().deployment_model == "unknown"


@pytest.mark.parametrize("category", sorted(SQL_INVENTORY_TYPES))
def test_sql_query_projection_supplies_the_classification_type(category):
    query = (Path(__file__).resolve().parents[1] / "kql" / f"{category}.kql").read_text()
    projection = query.rsplit("| project ", 1)[1].strip().split(", ")
    assert "type" in projection
    if category == "sql_databases_and_pools":
        assert "elasticPoolId" in projection
        assert "name !endswith '/master'" in query
    elif category == "sql_managed_instances_and_pools":
        assert "instancePoolId" in projection
    else:
        assert "billingResourceId" in projection


@pytest.mark.parametrize("delay", ["-1", "60", -1, 60, None])
def test_sql_detail_does_not_infer_compute_mode_from_auto_pause(delay):
    row = {
        "id": f"{_RESOURCE_PREFIX}/Microsoft.Sql/servers/server/databases/app",
        "type": "microsoft.sql/servers/databases",
        "elasticPoolId": "",
        "autoPauseDelay": delay,
    }
    report = build_report(["sub-1"], 0, {"sql_databases_and_pools": [row]}, {})
    line = report.tier_a_categories[0].lines[0]
    assert line.detail.startswith("SQL Database (single)")
    assert "serverless" not in line.detail
    assert "provisioned" not in line.detail
    assert "optimization eligibility not assessed" in line.detail


def test_sql_vm_costs_still_match_underlying_compute_without_claiming_savings():
    compute_id = f"{_RESOURCE_PREFIX}/Microsoft.Compute/virtualMachines/vm"
    row = {
        "id": f"{_RESOURCE_PREFIX}/Microsoft.SqlVirtualMachine/sqlVirtualMachines/vm",
        "name": "vm",
        "type": "microsoft.sqlvirtualmachine/sqlvirtualmachines",
        "subscriptionId": "sub-1",
        "billingResourceId": compute_id,
    }
    report = build_report(["sub-1"], 25, {"sql_virtual_machines": [row]}, {compute_id.lower(): 25})
    category = report.tier_a_categories[0]
    assert category.impact_type == "inventory"
    assert category.monthly_total == 25
    assert category.lines[0].monthly_cost == 25
    assert category.lines[0].sql_context.compute_resource_id == compute_id
    assert "compute resource linked by ID" in category.lines[0].detail