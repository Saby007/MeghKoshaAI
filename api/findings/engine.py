"""Deterministic findings engine - all dollar arithmetic happens here, in plain
Python, never delegated to the LLM. A category with zero matching Resource Graph
rows is OMITTED (not shown as zero), so the Cost Agent never even sees an empty
category to narrate or fabricate. Ported from the original browser-side
web/src/findings/engine.ts (removed - Resource Graph queries now run server-side,
see services/arm_client.py).
"""

from .models import AssessmentReport, FindingCategorySummary, FindingLine
from .sql_inventory import classify_sql_resource, is_system_sql_database, sql_resource_detail
from .idle_evidence import assess_idle_evidence

CATEGORY_DISPLAY_NAMES = {
    "unattached_disks": "Unattached managed disks",
    "stopped_vms": "VMs stopped but not deallocated",
    "idle_public_ips": "Idle / unattached public IPs",
    "empty_backend_pools": "Application Gateways with empty backend pool",
    "empty_load_balancer_backend_pools": "Load Balancers with empty backend pools",
    "idle_virtual_network_gateways": "Virtual Network Gateways with zero monthly traffic",
    "idle_nat_gateways": "NAT Gateways with zero monthly traffic",
    "idle_expressroute_circuits": "ExpressRoute circuits with zero monthly traffic",
    "old_snapshots": "Snapshots older than the stale threshold",
    "unattached_network_interfaces": "Unattached network interfaces",
    "unassociated_network_security_groups": "Network security groups with no association",
    "unassociated_route_tables": "Route tables with no subnet association",
    "empty_availability_sets": "Empty availability sets",
    "deallocated_virtual_machines": "Deallocated virtual machines",
    "zero_instance_vm_scale_sets": "Virtual machine scale sets with zero instances",
    "empty_app_service_plans": "App Service plans with no apps",
    "stopped_web_apps": "Stopped web apps and function apps",
    "empty_virtual_networks": "Virtual networks with no subnets in use",
    "disconnected_private_endpoints": "Private endpoints not in an approved state",
    "stopped_aks_clusters": "Stopped AKS clusters",
    "empty_resource_groups": "Empty resource groups",
    "old_custom_images": "Custom images older than the stale threshold",
    "sql_databases_and_pools": "SQL databases, elastic pools, and logical servers",
    "sql_managed_instances_and_pools": "SQL Managed Instances and instance pools",
    "sql_virtual_machines": "SQL Server virtual machines",
    "compute_ahb_candidates": "Windows Server Azure Hybrid Benefit candidates",
    "ai_cognitive_accounts": "Azure AI Services and OpenAI accounts",
    "ai_foundry_projects": "Microsoft Foundry projects",
    "ai_ml_workspaces": "Azure Machine Learning workspaces",
    "ai_search_services": "Azure AI Search services",
}

COST_AT_RISK_CATEGORIES = {
    "empty_backend_pools",
    "empty_load_balancer_backend_pools",
    "idle_virtual_network_gateways",
    "idle_nat_gateways",
    "idle_expressroute_circuits",
    "empty_app_service_plans",
}

INVENTORY_CATEGORIES = {
    "sql_databases_and_pools",
    "sql_managed_instances_and_pools",
    "sql_virtual_machines",
    "compute_ahb_candidates",
    "ai_cognitive_accounts",
    "ai_foundry_projects",
    "ai_ml_workspaces",
    "ai_search_services",
    "unattached_network_interfaces",
    "unassociated_network_security_groups",
    "unassociated_route_tables",
    "empty_availability_sets",
    "deallocated_virtual_machines",
    "zero_instance_vm_scale_sets",
    "stopped_web_apps",
    "empty_virtual_networks",
    "disconnected_private_endpoints",
    "stopped_aks_clusters",
    "empty_resource_groups",
    "old_custom_images",
}


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _line_detail(category: str, row: dict) -> str:
    if category == "unattached_disks":
        return f"{row.get('sizeGb', '?')} GB, SKU {row.get('sku', '?')}"
    if category == "stopped_vms":
        return f"VM size {row.get('vmSize', '?')}, power state {row.get('powerState', '?')}"
    if category == "idle_public_ips":
        return f"SKU {row.get('sku', '?')}, allocation {row.get('allocationMethod', '?')}"
    if category == "old_snapshots":
        return f"{row.get('ageDays', '?')} days old, {row.get('sizeGb', '?')} GB"
    if category == "empty_backend_pools":
        return "Application Gateway backend pools contain no addresses"
    if category == "unattached_network_interfaces":
        return f"No VM or private endpoint association; {row.get('ipConfigurationCount', 0)} IP configuration(s)"
    if category == "unassociated_network_security_groups":
        return f"No NIC or subnet association; {row.get('ruleCount', 0)} custom rule(s)"
    if category == "unassociated_route_tables":
        return f"No subnet association; {row.get('routeCount', 0)} route(s)"
    if category == "empty_availability_sets":
        return "No virtual machines are associated with this availability set"
    if category == "deallocated_virtual_machines":
        return f"VM size {row.get('vmSize', '?')}, deallocated; attached disks can still incur cost"
    if category == "zero_instance_vm_scale_sets":
        return f"SKU {row.get('sku', '?')}, configured capacity {row.get('capacity', 0)}"
    if category == "empty_app_service_plans":
        return f"SKU {row.get('sku') or row.get('tier') or '?'}, {row.get('workers', 0)} worker(s), no apps"
    if category == "stopped_web_apps":
        return f"State {row.get('state', '?')}, kind {row.get('kind', '?')}; review the hosting plan separately"
    if category == "empty_virtual_networks":
        return f"{row.get('subnetCount', 0)} subnet(s), none with IP configurations"
    if category == "disconnected_private_endpoints":
        return f"Private link connection state {row.get('connectionStatus') or 'not reported'}"
    if category == "stopped_aks_clusters":
        return f"Power state {row.get('powerState', '?')}, Kubernetes {row.get('kubernetesVersion', '?')}"
    if category == "empty_resource_groups":
        return "Resource group contains no Resource Graph resources"
    if category == "old_custom_images":
        return f"{row.get('ageDays', '?')} days old, type {row.get('imageType', '?')}"
    if category == "empty_load_balancer_backend_pools":
        return f"SKU {row.get('sku', '?')}, {row.get('backendPoolCount', 0)} backend pools, no backend addresses"
    if category == "idle_virtual_network_gateways":
        return f"SKU {row.get('sku', '?')}, generation {row.get('generation', '?')}, zero tunnel traffic across {row.get('metricDays', '?')} days"
    if category == "idle_nat_gateways":
        return f"SKU {row.get('sku', '?')}, {row.get('subnetCount', '?')} attached subnets, zero bytes and packets across {row.get('metricDays', '?')} days"
    if category == "idle_expressroute_circuits":
        return f"{row.get('bandwidthInMbps', '?')} Mbps, {row.get('skuTier', '?')} {row.get('skuFamily', '?')}, zero bidirectional traffic across {row.get('metricDays', '?')} days"
    if category == "compute_ahb_candidates":
        return f"Windows Server {row.get('offer') or ''} {row.get('imageSku') or ''}, VM size {row.get('vmSize') or '?'}, licenseType {row.get('licenseType') or 'not set'}; confirm qualifying Software Assurance or subscription licenses"
    if category == "ai_cognitive_accounts":
        return f"{row.get('resourceKind', 'Azure AI account')}, kind {row.get('accountKind') or '?'}, SKU {row.get('skuName') or '?'}; utilization metrics are not assessed in this increment"
    if category == "ai_foundry_projects":
        return f"Microsoft Foundry project under {row.get('parentAccountId') or 'its parent account'}; parent account billing is not duplicated"
    if category == "ai_ml_workspaces":
        return f"Azure Machine Learning {row.get('workspaceKind') or 'workspace'}, SKU {row.get('skuName') or '?'}; attached compute utilization is deferred"
    if category == "ai_search_services":
        return f"Azure AI Search SKU {row.get('skuName') or '?'}, {row.get('replicaCount') or 0} replica(s), {row.get('partitionCount') or 0} partition(s); query utilization is deferred"
    return ""


def build_report(
    subscription_ids: list[str],
    total_monthly_spend: float,
    resource_graph_rows: dict[str, list[dict]],
    cost_by_resource_id: dict[str, float],
    cost_evidence_by_resource_id: dict[str, list[dict]] | None = None,
    subscription_names: dict[str, str] | None = None,
    focus_pricing_evidence_by_resource_id: dict[str, list[dict]] | None = None,
) -> AssessmentReport:
    cost_evidence_by_resource_id = cost_evidence_by_resource_id or {}
    focus_pricing_evidence_by_resource_id = focus_pricing_evidence_by_resource_id or {}
    subscription_names = subscription_names or {}
    tier_a_categories: list[FindingCategorySummary] = []

    for category, rows in resource_graph_rows.items():
        lines: list[FindingLine] = []
        for row in rows:
            if is_system_sql_database(category, row):
                continue
            sql_context = classify_sql_resource(category, row)
            idle_evidence = assess_idle_evidence(category, row)
            resource_id = str(row.get("id", "")).lower()
            billing_resource_id = str(row.get("billingResourceId") or resource_id).lower()
            subscription_id = str(row.get("subscriptionId", ""))
            suppress_cost = bool(row.get("suppressCost"))
            monthly_cost = None if suppress_cost else cost_by_resource_id.get(billing_resource_id)
            evidence_type = row.get("evidenceType") or (
                "verified_cost" if monthly_cost is not None else "inventory_candidate"
            )
            lines.append(
                FindingLine(
                    category=category,
                    resource_id=str(row.get("id", "")),
                    resource_name=str(row.get("name", "")),
                    subscription_id=subscription_id,
                    subscription_name=subscription_names.get(subscription_id, subscription_id),
                    monthly_cost=monthly_cost,
                    confidence=float(row.get("confidence") or (0.9 if monthly_cost is not None else 0.5)),
                    evidence_type=evidence_type,
                    detail=(sql_resource_detail(sql_context, row) if sql_context else _line_detail(category, row))
                    + (f"; {idle_evidence.reason}" if idle_evidence else ""),
                    cost_evidence=[] if suppress_cost else cost_evidence_by_resource_id.get(billing_resource_id, []),
                    focus_pricing_evidence=(
                        [] if suppress_cost else focus_pricing_evidence_by_resource_id.get(billing_resource_id, [])
                    ),
                    sql_context=sql_context,
                    idle_evidence=idle_evidence,
                )
            )

        # Hard rule: no matching resources -> the category is omitted entirely.
        if not lines:
            continue

        monthly_total = sum(line.monthly_cost or 0 for line in lines)
        tier_a_categories.append(
            FindingCategorySummary(
                category=category,
                display_name=CATEGORY_DISPLAY_NAMES.get(category, category),
                count=len(lines),
                monthly_total=_round2(monthly_total),
                annual_total=_round2(monthly_total * 12),
                impact_type=(
                    "inventory"
                    if category in INVENTORY_CATEGORIES
                    else "cost_at_risk"
                    if category in COST_AT_RISK_CATEGORIES
                    else "potential_savings"
                ),
                lines=lines,
            )
        )

    return AssessmentReport(
        subscriptions=subscription_ids,
        total_monthly_spend=_round2(total_monthly_spend),
        tier_a_categories=tier_a_categories,
    )
