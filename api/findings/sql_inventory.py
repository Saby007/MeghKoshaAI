import re

from .models import SqlResourceContext
from .sql_analysis import analyze_sql_resource


SQL_INVENTORY_TYPES = {
    "sql_databases_and_pools": {
        "microsoft.sql/servers",
        "microsoft.sql/servers/databases",
        "microsoft.sql/servers/elasticpools",
    },
    "sql_managed_instances_and_pools": {
        "microsoft.sql/managedinstances",
        "microsoft.sql/instancepools",
    },
    "sql_virtual_machines": {"microsoft.sqlvirtualmachine/sqlvirtualmachines"},
}


def _related_resource_id(row: dict, field: str, resource_path: str) -> str | None:
    value = row.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip("/")
    match = re.fullmatch(
        rf"/subscriptions/([^/]+)/resourceGroups/[^/]+/providers/{resource_path}",
        value,
        flags=re.IGNORECASE,
    )
    if not match or match.group(1).casefold() != str(row.get("subscriptionId") or "").casefold():
        return None
    return value


def is_system_sql_database(category: str, row: dict) -> bool:
    if category != "sql_databases_and_pools" or str(row.get("type") or "").casefold() != "microsoft.sql/servers/databases":
        return False
    return any(
        str(row.get(field) or "").strip().rstrip("/").rsplit("/", 1)[-1].casefold() == "master"
        for field in ("name", "id")
    )


def classify_sql_resource(category: str, row: dict) -> SqlResourceContext | None:
    if category not in SQL_INVENTORY_TYPES:
        return None
    resource_type = str(row.get("type") or "").strip().casefold()
    context = SqlResourceContext(resource_type=resource_type)
    if not resource_type:
        context.classification_reason = "Resource type is missing; display labels are not classification evidence"
        context.evidence_gaps.append("resource_type_missing")
        return context
    if resource_type not in SQL_INVENTORY_TYPES[category]:
        context.classification_reason = "Resource type is unsupported or does not match the inventory category"
        context.evidence_gaps.append("resource_type_unsupported_or_mismatched")
        return context

    if resource_type == "microsoft.sql/servers/databases":
        if str(row.get("skuTier") or "").casefold() == "datawarehouse":
            context.classification_reason = "Dedicated SQL pool analysis is outside the enabled inventory models"
            context.evidence_gaps.append("dedicated_sql_pool_not_supported")
            return context
        pool_id = row.get("elasticPoolId")
        if not isinstance(pool_id, str):
            context.classification_reason = "Elastic-pool membership was not reported"
            context.evidence_gaps.append("elastic_pool_membership_missing")
            return context
        if pool_id.strip():
            context.pool_resource_id = _related_resource_id(
                row, "elasticPoolId", r"Microsoft\.Sql/servers/[^/]+/elasticPools/[^/]+"
            )
            database_server = str(row.get("id") or "").casefold().rpartition("/databases/")[0]
            pool_server = (context.pool_resource_id or "").casefold().rpartition("/elasticpools/")[0]
            if not context.pool_resource_id or not database_server or database_server != pool_server:
                context.pool_resource_id = None
                context.classification_reason = "Elastic-pool reference is malformed or belongs to a different server"
                context.evidence_gaps.append("elastic_pool_reference_invalid")
                return context
        context.deployment_model = "pooled_database" if context.pool_resource_id else "single_database"
        context.classification_reason = "Database type and explicit elastic-pool membership metadata"
    elif resource_type == "microsoft.sql/servers/elasticpools":
        context.deployment_model = "elastic_pool"
        context.classification_reason = "Elastic-pool resource type"
    elif resource_type == "microsoft.sql/servers":
        context.deployment_model = "logical_server"
        context.classification_reason = "Logical host, not an independently sized database"
    elif resource_type == "microsoft.sql/managedinstances":
        context.deployment_model = "managed_instance"
        context.classification_reason = "Managed-instance resource type"
        pool_id = row.get("instancePoolId")
        if not isinstance(pool_id, str):
            context.evidence_gaps.append("instance_pool_membership_missing")
        elif pool_id.strip():
            context.pool_resource_id = _related_resource_id(row, "instancePoolId", r"Microsoft\.Sql/instancePools/[^/]+")
            if not context.pool_resource_id:
                context.evidence_gaps.append("instance_pool_reference_invalid")
    elif resource_type == "microsoft.sql/instancepools":
        context.deployment_model = "instance_pool"
        context.classification_reason = "Managed-instance pool resource type"
    elif resource_type == "microsoft.sqlvirtualmachine/sqlvirtualmachines":
        context.deployment_model = "sql_vm"
        context.classification_reason = "SQL VM registration; underlying compute evidence must be assessed separately"
        context.compute_resource_id = _related_resource_id(row, "billingResourceId", r"Microsoft\.Compute/virtualMachines/[^/]+")
        if not context.compute_resource_id:
            context.evidence_gaps.append("compute_resource_id_missing_or_invalid")
    context.optimization_checks = analyze_sql_resource(context, row)
    return context


def sql_resource_detail(context: SqlResourceContext, row: dict) -> str:
    labels = {
        "single_database": "SQL Database (single)",
        "pooled_database": "SQL Database (elastic pool member)",
        "elastic_pool": "SQL elastic pool",
        "managed_instance": "SQL Managed Instance",
        "instance_pool": "SQL Managed Instance pool",
        "sql_vm": "SQL Server on Azure VM",
        "logical_server": "SQL logical server (host only)",
        "unknown": "SQL deployment model unclassified",
    }
    label = labels[context.deployment_model]
    if context.deployment_model == "unknown":
        return f"{label}; {context.classification_reason}; optimization eligibility not assessed"
    if context.deployment_model == "logical_server":
        return f"{label}; assess capacity at the database or pool level"
    if context.deployment_model == "sql_vm":
        identity = "compute resource linked by ID" if context.compute_resource_id else "compute resource link unavailable"
        return (
            f"{label}, image {row.get('offer') or '?'} {row.get('imageSku') or ''}, "
            f"license {row.get('sqlServerLicenseType') or 'not reported'}; {identity}; "
            "workload utilization and license eligibility not assessed"
        )
    sku = row.get("skuName") or row.get("skuTier") or "not reported"
    license_type = row.get("licenseType") or "not reported"
    return f"{label}, SKU {sku}, license {license_type}; workload utilization and optimization eligibility not assessed"