"""Assembles the full multi-tab cost assessment report from the same underlying
Resource Graph findings, Cost Management data, and Azure Advisor recommendations
already collected for POST /api/assessment - mirrors the original Excel workbook's
9 tabs (PROJECT.md Part I): Executive Summary, Savings Roadmap, Subscription Breakdown,
Compute/Storage/Network Optimization, Advisor Reconciliation, Governance & Risk,
Action Plan. All dollar figures and counts come from real Resource Graph / Cost
Management / Advisor data - only the per-category guidance text below is static.
"""

import re
from dataclasses import asdict

from findings.engine import build_report
from findings.models import FindingCategorySummary
from services.focus_cost_reader import FocusCostData, FocusReconciliation, _commitment_kind, _display_tag_key
from services.focus_history_reader import FocusHistoryData

from .remediation import DEFINITIONS as REMEDIATION_DEFINITIONS, build_remediation_plan
from .cost_details import build_cost_details

from .models import (
    ActionPlanItem,
    AIModelDeploymentUsage,
    AIOptimizationOpportunity,
    AIUsageSummary,
    AdvisorMeasure,
    AdvisorReconciliation,
    AdvisorScoreSummary,
    CommitmentBenefitBreakdown,
    CommitmentInsights,
    CommitmentMonthPoint,
    CommitmentSummary,
    ChargebackRow,
    ChargebackSummary,
    ComplianceRow,
    ComplianceSummary,
    ComputeCoverageSplit,
    CostHierarchyItem,
    DataCompleteness,
    DomainAdvisorRecommendation,
    DomainSummary,
    ExecutiveFinding,
    ExecutiveSummary,
    ExtendedSupportRow,
    ExtendedSupportSummary,
    FullReport,
    GovernanceRow,
    MonthlySpendPoint,
    OffHoursSavingsRow,
    OffHoursSavingsSummary,
    DailyCostPoint,
    DailyCostTrendSummary,
    TagDailyCostSeries,
    TagDailyCostTrendSummary,
    OperationalSignal,
    RegionSpendSummary,
    ReportMetadata,
    PricingSubscriptionSummary,
    PricingSummary,
    SavingsRoadmapItem,
    ServiceSpendSummary,
    SpendCategorySummary,
    SpendHistorySummary,
    StorageAccountTierAnalysis,
    StorageOptimizationSummary,
    StorageTierVolume,
    SubscriptionBreakdownRow,
    SubscriptionReference,
    TagCostSummary,
    TagDimensionCost,
    TagValueCost,
)

CATEGORY_DOMAIN = {
    "old_snapshots": "storage",
    "unattached_disks": "storage",
    "stopped_vms": "compute",
    "idle_public_ips": "network",
    "empty_backend_pools": "network",
    "empty_load_balancer_backend_pools": "network",
    "idle_virtual_network_gateways": "network",
    "idle_nat_gateways": "network",
    "idle_expressroute_circuits": "network",
    "unattached_network_interfaces": "network",
    "unassociated_network_security_groups": "network",
    "unassociated_route_tables": "network",
    "empty_availability_sets": "compute",
    "deallocated_virtual_machines": "compute",
    "zero_instance_vm_scale_sets": "compute",
    "empty_app_service_plans": "compute",
    "stopped_web_apps": "compute",
    "empty_virtual_networks": "network",
    "disconnected_private_endpoints": "network",
    "stopped_aks_clusters": "compute",
    "old_custom_images": "storage",
    "sql_databases_and_pools": "sql",
    "sql_managed_instances_and_pools": "sql",
    "sql_virtual_machines": "sql",
    "compute_ahb_candidates": "compute",
    "ai_cognitive_accounts": "ai",
    "ai_foundry_projects": "ai",
    "ai_ml_workspaces": "ai",
    "ai_search_services": "ai",
}
DOMAIN_SERVICE_FAMILY = {"compute": "Compute", "storage": "Storage", "network": "Networking"}
DOMAIN_CONSUMED_SERVICES = {
    "sql": {"microsoft.sql", "microsoft.sqlvirtualmachine"},
    "ai": {"microsoft.cognitiveservices", "microsoft.machinelearningservices", "microsoft.search"},
}
DOMAIN_DISPLAY_NAMES = {
    "compute": "Compute Optimization",
    "storage": "Storage Optimization",
    "network": "Network Optimization",
    "sql": "Azure SQL Optimization",
    "ai": "AI Optimization",
}

# Generic, category-level guidance - not a fabricated dollar figure or count; every
# $ and count in the report comes from live Resource Graph / Cost Management data.
RECOMMENDED_ACTIONS = {
    "old_snapshots": "Delete after owner sign-off - no restore SLA applies at this age.",
    "stopped_vms": "Deallocate or delete after confirming no pending maintenance window.",
    "unattached_disks": "Delete after taking a safety snapshot.",
    "empty_backend_pools": "Inspect routing dependencies and confirm whether the gateway or empty pools are still required.",
    "idle_public_ips": "Release after confirming no pending failover/DR use.",
    "empty_load_balancer_backend_pools": "Inspect frontend rules and confirm whether the Load Balancer is still required.",
    "idle_virtual_network_gateways": "Validate connectivity ownership and planned recovery use before changing the gateway.",
    "idle_nat_gateways": "Validate attached subnet egress requirements before changing the NAT Gateway.",
    "idle_expressroute_circuits": "Confirm circuit ownership, peerings, and failover requirements before cancellation or resizing.",
}
ACTION_PLAN_VERBS = {
    "old_snapshots": "Delete {count} snapshots older than 12 months",
    "stopped_vms": "Deallocate the {count} VMs that are stopped but still billing",
    "unattached_disks": "Delete {count} unattached managed disks",
    "empty_backend_pools": "Review {count} Application Gateways with empty backend pools",
    "idle_public_ips": "Release {count} idle/unattached public IPs",
    "empty_load_balancer_backend_pools": "Review {count} Load Balancers with empty backend pools",
    "idle_virtual_network_gateways": "Review {count} zero-traffic Virtual Network Gateways",
    "idle_nat_gateways": "Review {count} zero-traffic NAT Gateways",
    "idle_expressroute_circuits": "Review {count} zero-traffic ExpressRoute circuits",
}

FINDING_HEADLINES = {
    "old_snapshots": "Snapshot sprawl remains an avoidable storage cost",
    "stopped_vms": "Stopped VMs remain allocated and billable",
    "unattached_disks": "Unattached managed disks remain in the estate",
    "idle_public_ips": "Idle public IP addresses continue to incur cost",
    "empty_backend_pools": "Application Gateways have no active backend capacity",
    "empty_load_balancer_backend_pools": "Load Balancers have no active backend capacity",
    "idle_virtual_network_gateways": "Virtual Network Gateways show no traffic for the closed period",
    "idle_nat_gateways": "NAT Gateways show no traffic for the closed period",
    "idle_expressroute_circuits": "ExpressRoute circuits show no traffic for the closed period",
}

SERVICE_DISPLAY_NAMES = {
    "microsoft.app": "Azure Container Apps",
    "microsoft.cognitiveservices": "Azure AI Services",
    "microsoft.compute": "Virtual Machines & Compute",
    "microsoft.containerregistry": "Azure Container Registry",
    "microsoft.insights": "Azure Monitor",
    "microsoft.machinelearningservices": "Azure Machine Learning",
    "microsoft.network": "Azure Networking",
    "microsoft.operationalinsights": "Log Analytics",
    "microsoft.search": "Azure AI Search",
    "microsoft.sql": "Azure SQL",
    "microsoft.storage": "Azure Storage",
    "microsoft.web": "Azure App Service & Static Web Apps",
}
SPEND_CATEGORIES = ("Compute", "Storage", "Networking", "Databases", "AI/ML", "Other")


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _service_display_name(service_name: str) -> str:
    mapped = SERVICE_DISPLAY_NAMES.get(service_name.lower())
    if mapped:
        return mapped
    short_name = re.sub(r"^Microsoft\.", "", service_name, flags=re.IGNORECASE)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", short_name).replace("_", " ")


def _top_services(service_spend: dict[str, float], total_spend: float) -> list[ServiceSpendSummary]:
    contributors = sorted(
        ((name, spend) for name, spend in service_spend.items() if spend > 0),
        key=lambda item: (-item[1], item[0].lower()),
    )[:10]
    return [
        ServiceSpendSummary(
            rank=rank,
            service_name=name,
            display_name=_service_display_name(name),
            monthly_spend=_round2(spend),
            pct_of_total=(spend / total_spend) if total_spend > 0 else 0.0,
        )
        for rank, (name, spend) in enumerate(contributors, start=1)
    ]


def _spend_category(resource_type: str, service_category: str = "") -> str:
    value = f"{resource_type} {service_category}".lower()
    if any(token in value for token in ("cognitiveservices", "machinelearning", "openai", "microsoft.search", "ai and machine learning")):
        return "AI/ML"
    if any(token in value for token in ("microsoft.sql", "documentdb", "dbfor", "database", "cosmos")):
        return "Databases"
    if any(token in value for token in ("microsoft.storage", "storage")):
        return "Storage"
    if any(token in value for token in ("microsoft.network", "network")):
        return "Networking"
    if any(token in value for token in ("microsoft.compute", "microsoft.web", "microsoft.app", "containerservice", "virtual machine", "compute")):
        return "Compute"
    return "Other"


def _spend_categories(provider_spend: dict[str, float], total_spend: float) -> list[SpendCategorySummary]:
    totals = {category: 0.0 for category in SPEND_CATEGORIES}
    for provider, spend in provider_spend.items():
        totals[_spend_category(provider)] += spend
    return [
        SpendCategorySummary(
            category=category,
            monthly_spend=_round2(totals[category]),
            pct_of_total=(totals[category] / total_spend) if total_spend else 0.0,
        )
        for category in SPEND_CATEGORIES
    ]


def _spend_history(
    history: FocusHistoryData | None,
    focus_data: FocusCostData | None,
) -> SpendHistorySummary:
    monthly: dict[str, dict[str, float]] = {}
    monthly_subscriptions: dict[str, dict[str, float]] = {}
    monthly_subscription_categories: dict[str, dict[str, dict[str, float]]] = {}

    def add_cost(month: str, subscription_id: str, category: str, cost: float) -> None:
        values = monthly.setdefault(month, {value: 0.0 for value in SPEND_CATEGORIES})
        values[category] += cost
        subscription_values = monthly_subscriptions.setdefault(month, {})
        subscription_values[subscription_id] = subscription_values.get(subscription_id, 0.0) + cost
        category_values = monthly_subscription_categories.setdefault(month, {}).setdefault(
            subscription_id,
            {value: 0.0 for value in SPEND_CATEGORIES},
        )
        category_values[category] += cost

    if history:
        for record in history.records:
            month = record.date[:7]
            category = _spend_category(record.resource_type, record.service_category or record.service_name)
            add_cost(month, record.subscription_id, category, record.effective_cost)
    if focus_data:
        monthly.pop(focus_data.period, None)
        monthly_subscriptions.pop(focus_data.period, None)
        monthly_subscription_categories.pop(focus_data.period, None)
        for item in focus_data.resource_costs:
            add_cost(
                focus_data.period,
                item.subscription_id,
                _spend_category(item.resource_type),
                item.effective_cost,
            )
        current_values = {value: 0.0 for value in SPEND_CATEGORIES}
        for provider, spend in focus_data.provider_spend.items():
            current_values[_spend_category(provider)] += spend
        monthly[focus_data.period] = current_values
        monthly_subscriptions[focus_data.period] = dict(focus_data.effective_cost_by_subscription)
    points = [
        MonthlySpendPoint(
            month=month,
            total=_round2(sum(values.values())),
            category_spend={category: _round2(values[category]) for category in SPEND_CATEGORIES},
            subscription_spend={
                subscription_id: _round2(cost)
                for subscription_id, cost in monthly_subscriptions.get(month, {}).items()
            },
            subscription_category_spend={
                subscription_id: {
                    category: _round2(cost)
                    for category, cost in category_values.items()
                }
                for subscription_id, category_values in monthly_subscription_categories.get(month, {}).items()
            },
        )
        for month, values in sorted(monthly.items())[-12:]
    ]
    count = len(points)
    return SpendHistorySummary(
        status="complete" if count == 12 else "partial",
        status_message=(
            "12 complete contiguous FOCUS export months."
            if count == 12
            else f"{count} complete FOCUS export month{'s' if count != 1 else ''} available; the chart will fill as exports accrue."
        ),
        months=points,
    )


def _daily_cost_trend(history: FocusHistoryData | None, days: int = 180) -> DailyCostTrendSummary:
    if not history or not history.records:
        return DailyCostTrendSummary(
            status="unavailable",
            statusMessage="Daily FOCUS export history is not available yet.",
            days=[],
        )
    daily_totals: dict[str, float] = {}
    for record in history.records:
        daily_totals[record.date] = daily_totals.get(record.date, 0.0) + record.effective_cost
    from datetime import date, timedelta

    end = date.fromisoformat(history.history_end)
    start = max(date.fromisoformat(history.history_start), end - timedelta(days=max(days, 1) - 1))
    recent_dates = [str(start + timedelta(days=index)) for index in range((end - start).days + 1)]
    points = [
        DailyCostPoint(
            date=value,
            totalCost=_round2(daily_totals.get(value, 0)),
            averageHourlyCost=round(daily_totals.get(value, 0) / 24, 6),
        )
        for value in recent_dates
    ]
    return DailyCostTrendSummary(
        status="complete" if len(points) >= days else "partial",
        statusMessage=(
            f"Last {len(points)} complete FOCUS export day{'s' if len(points) != 1 else ''}; "
            "average hourly cost is each day's total divided by 24, not a real hourly billing figure."
        ),
        days=points,
    )


# Azure auto-injects some system properties into the same Tags JSON blob as real
# user tags (e.g. "virtualMachineProfileTimeCreated" on VMSS instances). Their
# values are timestamps, so every value is near-unique - never a meaningful cost
# category, so these are excluded from the tag trend regardless of which key
# they're under.
_TIMESTAMP_LIKE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}|^\d{1,2}/\d{1,2}/\d{4}[, ]+\d{1,2}:\d{2}",
    re.IGNORECASE,
)


def _looks_like_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP_LIKE_PATTERN.match(value.strip()))


# Azure/AKS also auto-injects tags with well-known key prefixes onto resources it
# manages on the user's behalf (e.g. "aks-managed-cluster-name" on every resource in
# an AKS node resource group) - these identify platform-managed infrastructure, not
# a business cost category, and their broad coverage would otherwise out-rank a
# tenant's real tags on richness alone.
_PLATFORM_TAG_KEY_PREFIXES = ("aks-managed-", "k8s-azure-", "kubernetes.io-", "hidden-")


def _is_platform_tag_key(key: str) -> bool:
    return key.startswith(_PLATFORM_TAG_KEY_PREFIXES)


def _tag_daily_cost_trend(
    history: FocusHistoryData | None,
    top_n: int = 100,
    days: int = 90,
) -> TagDailyCostTrendSummary:
    # Combine every tag key actually populated (minus known platform/system noise)
    # into one ranking instead of picking a single "best" key - a tenant's tags
    # rarely live under one key, and restricting to one hid the rest from the user.
    if not history or not history.records:
        return TagDailyCostTrendSummary(
            status="unavailable",
            statusMessage="Daily FOCUS export history is not available yet.",
            tagKey="",
            availableTagValues=[],
            series=[],
        )
    totals: dict[tuple[str, str], float] = {}
    daily: dict[tuple[str, str], dict[str, float]] = {}
    from datetime import date, timedelta

    end = date.fromisoformat(history.history_end)
    start = max(date.fromisoformat(history.history_start), end - timedelta(days=max(days, 1) - 1))
    window_dates = [str(start + timedelta(days=index)) for index in range((end - start).days + 1)]
    included_dates = set(window_dates)
    distribution: dict[tuple[tuple[str, str], ...], dict[str, float]] = {}
    for record in history.records:
        if record.date not in included_dates or record.effective_cost == 0:
            continue
        tags = tuple(sorted((key, value) for key, value in record.tags.items()
                            if value and not _is_platform_tag_key(key.casefold()) and not _looks_like_timestamp(value)))
        by_set = distribution.setdefault(tags, {})
        by_set[record.date] = by_set.get(record.date, 0.0) + record.effective_cost
        for key, value in tags:
            combo = (key, value)
            totals[combo] = totals.get(combo, 0.0) + record.effective_cost
            by_date = daily.setdefault(combo, {})
            by_date[record.date] = by_date.get(record.date, 0.0) + record.effective_cost
    ranked_combos = sorted(totals, key=lambda combo: totals[combo], reverse=True)
    top_combos = ranked_combos[:max(top_n, 0)]
    series = [
        TagDailyCostSeries(
            tagKey=_display_tag_key(key),
            tagValue=f"{_display_tag_key(key)}: {value}",
            totalCost=_round2(sum(_round2(value) for value in daily[(key, value)].values())),
            days=[
                DailyCostPoint(
                    date=day,
                    totalCost=_round2(daily[(key, value)].get(day, 0)),
                    averageHourlyCost=round(daily[(key, value)].get(day, 0) / 24, 6),
                )
                for day in window_dates
            ],
        )
        for key, value in top_combos
    ]
    contributing_keys = sorted({_display_tag_key(key) for key, _ in ranked_combos})
    ranked_sets = sorted(distribution, key=lambda tags: (-abs(sum(distribution[tags].values())), tags))
    distribution_rows = []
    for tags in ranked_sets[:10]:
        distribution_rows.append(("; ".join(f"{_display_tag_key(key)}: {value}" for key, value in tags) or "Untagged", distribution[tags]))
    if len(ranked_sets) > 10:
        other: dict[str, float] = {}
        for tags in ranked_sets[10:]:
            for day, amount in distribution[tags].items():
                other[day] = other.get(day, 0) + amount
        distribution_rows.append(("Other tag sets", other))
    distribution_series = [TagDailyCostSeries(
        tagKey="Tag set", tagValue=label,
        totalCost=_round2(sum(_round2(amount) for amount in values.values())),
        days=[DailyCostPoint(date=day, totalCost=_round2(values.get(day, 0)), averageHourlyCost=round(values.get(day, 0) / 24, 6)) for day in window_dates],
    ) for label, values in distribution_rows]
    return TagDailyCostTrendSummary(
        status="partial" if len(top_combos) < len(ranked_combos) else "complete" if distribution_series else "unavailable",
        statusMessage=(
            f"Individual tag comparisons overlap and must not be added together; showing {len(top_combos)} of {len(ranked_combos)} tag values. "
            "Tag-set distribution counts each billing row once, including untagged cost. Average hourly cost is derived from complete export days."
            + (" Resource-group fallback uses current tags, not historical group-tag snapshots." if any(record.tag_attribution_source == "exported_resource_tags_with_current_group_fallback" for record in history.records) else "")
        ) if distribution_series else "No cost rows were found in the available FOCUS history.",
        tagKey=", ".join(contributing_keys),
        availableTagValues=[item.tag_value for item in series],
        series=series,
        distributionSeries=distribution_series,
        windowDates=window_dates,
    )


def _cost_hierarchy(focus_data: FocusCostData | None, total_spend: float) -> list[CostHierarchyItem]:
    if not focus_data:
        return []
    return [
        CostHierarchyItem(
            subscription_id=item.subscription_id,
            subscription_name=item.subscription_name,
            resource_group=item.resource_group,
            resource_id=item.resource_id,
            resource_name=item.resource_name,
            resource_type=item.resource_type,
            monthly_spend=_round2(item.effective_cost),
            pct_of_total=(item.effective_cost / total_spend) if total_spend else 0.0,
        )
        for item in focus_data.resource_costs
        if item.effective_cost > 0
    ][:200]


def _region_spend(focus_data: FocusCostData | None, total_spend: float) -> list[RegionSpendSummary]:
    if not focus_data:
        return []
    return [
        RegionSpendSummary(
            region=region or "Unassigned",
            monthly_spend=_round2(spend),
            pct_of_total=(spend / total_spend) if total_spend else 0.0,
        )
        for region, spend in sorted(focus_data.region_spend.items(), key=lambda item: (-item[1], item[0]))
        if spend != 0
    ]


def _score_value(row: dict) -> float | None:
    properties = row.get("properties") or {}
    candidates = (
        properties.get("score"),
        properties.get("currentScore"),
        properties.get("percentageScore"),
        (properties.get("latestScore") or {}).get("score")
        if isinstance(properties.get("latestScore"), dict)
        else None,
        (properties.get("lastRefreshedScore") or {}).get("score")
        if isinstance(properties.get("lastRefreshedScore"), dict)
        else None,
    )
    for candidate in candidates:
        value = _optional_float(candidate)
        if value is not None:
            return value * 100 if 0 <= value <= 1 else value
    return None


def _category_score_rows(rows: list[dict]) -> list[dict]:
    normalized = []
    for row in rows:
        nested = (row.get("properties") or {}).get("value")
        if not isinstance(nested, list):
            normalized.append(row)
            continue
        for item in nested:
            item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            if not re.search(r"/providers/microsoft\.advisor/advisorscore/category/[^/]+$", item_id, re.IGNORECASE):
                continue
            normalized.append({**item, "subscriptionId": row.get("subscriptionId")})
    return normalized


def _advisor_score(rows: list[dict]) -> AdvisorScoreSummary:
    scored = [(row, _score_value(row)) for row in _category_score_rows(rows)]
    scored = [(row, value) for row, value in scored if value is not None]
    if not scored:
        return AdvisorScoreSummary(
            available=False,
            score=None,
            cost_score=None,
            monthly_change=None,
            subscription_count=0,
            status="Advisor Score was not returned for the selected subscriptions.",
        )
    overall = []
    cost = []
    subscriptions = set()
    for row, value in scored:
        subscriptions.add(str(row.get("subscriptionId") or ""))
        properties = row.get("properties") or {}
        label = f"{row.get('name', '')} {properties.get('category', '')} {properties.get('name', '')}".lower()
        if "cost" in label:
            cost.append(value)
        elif "overall" in label or "advisor" in label:
            overall.append(value)
    values = overall or [value for _, value in scored]
    return AdvisorScoreSummary(
        available=True,
        score=_round2(sum(values) / len(values)),
        cost_score=_round2(sum(cost) / len(cost)) if cost else None,
        monthly_change=None,
        subscription_count=len({value for value in subscriptions if value}),
        status="Mean of the latest subscription-level Advisor category scores returned by Azure Resource Graph.",
    )


def _storage_optimization(
    storage_accounts: list[dict],
    storage_metrics: dict[str, dict],
    subscription_names: dict[str, str],
    focus_data: FocusCostData | None,
) -> StorageOptimizationSummary:
    accounts: list[StorageAccountTierAnalysis] = []
    tier_totals: dict[str, float] = {}
    for row in storage_accounts:
        resource_id = str(row.get("id") or "").lower()
        metrics = storage_metrics.get(resource_id, {})
        tier_bytes = metrics.get("tierBytes") or {}
        for tier, byte_count in tier_bytes.items():
            tier_totals[str(tier)] = tier_totals.get(str(tier), 0.0) + float(byte_count)
        complete = bool(metrics.get("complete"))
        tracked = metrics.get("lastAccessTrackingEnabled")
        reads = _optional_float(metrics.get("readTransactions"))
        if not complete:
            access_pattern = "Blob metrics unavailable"
            recommended = "Enable blob capacity and transaction metrics; no tier change is recommended without evidence."
            evidence_status = "Telemetry required"
        elif tracked is not True:
            access_pattern = f"{reads:,.0f} blob reads in period" if reads is not None else "Read telemetry unavailable"
            recommended = "Enable last-access tracking and blob inventory before creating lifecycle rules."
            evidence_status = "Last-access evidence required"
        elif reads == 0:
            access_pattern = "No blob reads in closed period"
            recommended = "Review a lifecycle rule: Cool after 30d, Cold after 90d, Archive after 180d without access."
            evidence_status = "Lifecycle candidate"
        elif reads is not None and reads <= 4:
            access_pattern = f"{reads:,.0f} blob reads in closed period"
            recommended = "Review Cool or Cold lifecycle tiers by blob last-access time."
            evidence_status = "Lifecycle candidate"
        else:
            access_pattern = f"{reads:,.0f} blob reads in closed period" if reads is not None else "Read telemetry unavailable"
            recommended = "Retain active blobs online and tier only inactive blobs by last-access time."
            evidence_status = "Active account"
        accounts.append(
            StorageAccountTierAnalysis(
                storage_account_id=resource_id,
                storage_account_name=str(row.get("name") or resource_id.rsplit("/", 1)[-1]),
                subscription_id=str(row.get("subscriptionId") or ""),
                subscription_name=subscription_names.get(str(row.get("subscriptionId") or ""), str(row.get("subscriptionId") or "")),
                resource_group=str(row.get("resourceGroup") or ""),
                location=str(row.get("location") or "Unassigned"),
                current_tier=str(row.get("currentTier") or "Not set"),
                size_bytes=_optional_float(metrics.get("capacityBytes")),
                tier_volumes=[
                    StorageTierVolume(tier=str(tier), bytes=float(byte_count))
                    for tier, byte_count in sorted(tier_bytes.items())
                ],
                access_pattern=access_pattern,
                read_transactions=reads,
                recommended=recommended,
                estimated_saving_month=None,
                monthly_cost=(focus_data.effective_cost_by_resource_id.get(resource_id) if focus_data else None),
                evidence_status=evidence_status,
            )
        )
    current = [StorageTierVolume(tier=tier, bytes=value) for tier, value in sorted(tier_totals.items())]
    complete_accounts = sum(1 for account in accounts if account.evidence_status != "Telemetry required")
    return StorageOptimizationSummary(
        status=(
            "No storage accounts were found in the selected scope."
            if not accounts
            else f"{complete_accounts}/{len(accounts)} storage accounts have blob telemetry for the closed period. Savings remain unquantified until eligible blob volume and retrieval costs are known."
        ),
        accounts=accounts,
        current_tier_volumes=current,
        recommended_tier_volumes=[],
    )


def _ai_usage_summary(raw: dict | None) -> AIUsageSummary:
    raw = raw or {}
    deployments = [AIModelDeploymentUsage.model_validate(item) for item in raw.get("deployments") or []]
    opportunities: list[AIOptimizationOpportunity] = []
    for deployment in deployments:
        tokens = deployment.total_tokens_per_day
        name = deployment.deployment_name.lower()
        if tokens == 0:
            opportunities.append(AIOptimizationOpportunity(
                deploymentName=deployment.deployment_name,
                category="Idle deployment",
                recommendation="Confirm ownership and remove unused deployment capacity or quota allocation after validation.",
                evidence=f"Zero inference tokens per day across {deployment.evidence_status.lower()}.",
                priority="High",
            ))
        if deployment.trend_percentage is not None and deployment.trend_percentage > 0.5:
            opportunities.append(AIOptimizationOpportunity(
                deploymentName=deployment.deployment_name,
                category="Rapid usage growth",
                recommendation="Review quota, throttling, caching, and request-volume drivers before the next billing period.",
                evidence=f"Total token usage increased {deployment.trend_percentage * 100:.1f}% across equal seven-day windows.",
                priority="Medium",
            ))
        if tokens is not None and tokens >= 1_000_000 and any(marker in name for marker in ("dev", "test", "sandbox", "poc")):
            opportunities.append(AIOptimizationOpportunity(
                deploymentName=deployment.deployment_name,
                category="High non-production usage",
                recommendation="Validate whether non-production traffic needs production-scale token volume and retention.",
                evidence=f"Deployment name indicates non-production use and averages {tokens:,.0f} total tokens per day.",
                priority="Medium",
            ))
    eligible = int(raw.get("eligibleAccountCount") or 0)
    available = sum(1 for item in deployments if item.total_tokens_per_day is not None)
    status = (
        "No Azure OpenAI model deployments were found in the selected scope."
        if not deployments
        else f"{available}/{len(deployments)} deployments have complete Azure Monitor token evidence; cost/day awaits the pricing allocator."
    )
    if eligible and not deployments:
        status = f"{eligible} eligible Azure OpenAI account(s) were found, but deployment inventory or token metrics were unavailable."
    return AIUsageSummary(
        status=status,
        period_start=str(raw.get("periodStart") or ""),
        period_end=str(raw.get("periodEnd") or ""),
        deployments=sorted(deployments, key=lambda item: (item.total_tokens_per_day is None, -(item.total_tokens_per_day or 0), item.deployment_name.lower())),
        opportunities=sorted(opportunities, key=lambda item: ({"High": 0, "Medium": 1, "Low": 2}[item.priority], item.deployment_name.lower())),
    )


def _chargeback_summary(focus_data: FocusCostData | None, total_spend: float) -> ChargebackSummary:
    if not focus_data or not focus_data.tag_spend:
        return ChargebackSummary(
            available=False,
            status="FOCUS tag allocation is unavailable for this report period.",
            allocated_cost=0,
            unallocated_cost=0,
            allocation_percentage=0,
            rows=[],
        )
    rows = [
        ChargebackRow(
            dimension=dimension,
            value=value,
            monthly_cost=_round2(cost),
            pct_of_total=(cost / total_spend) if total_spend else 0.0,
        )
        for dimension, values in focus_data.tag_spend.items()
        for value, cost in sorted(values.items(), key=lambda item: (-item[1], item[0].lower()))
        if cost != 0
    ]
    primary = focus_data.tag_spend.get("Team") or {}
    allocated = sum(cost for value, cost in primary.items() if value != "Unallocated")
    unallocated = primary.get("Unallocated", 0.0)
    return ChargebackSummary(
        available=bool(rows),
        status=(
            "Team, Department, and Project allocations use case-insensitive FOCUS tags; missing tags remain Unallocated."
            if rows
            else "No Team, Department, or Project tag values were present."
        ),
        allocated_cost=_round2(allocated),
        unallocated_cost=_round2(unallocated),
        allocation_percentage=(allocated / total_spend) if total_spend else 0.0,
        rows=rows,
    )


def _tag_cost_summary(
    focus_data: FocusCostData | None,
    total_spend: float,
    growth_rate: float | None,
) -> TagCostSummary:
    """Cost grouped by every FOCUS tag key actually found (case-insensitive) - on a
    resource or inherited from its resource group - with a next-month forecast derived
    from the same MoM total-spend growth rate used for the executive summary."""
    if not focus_data or not focus_data.tag_spend:
        return TagCostSummary(
            available=False,
            status="FOCUS tag data was not collected.",
            totalSpend=0,
            growthRate=None,
            dimensions=[],
        )
    dimensions = []
    for tag_key, values in sorted(focus_data.tag_spend.items()):
        rows = [
            TagValueCost(
                value=value,
                monthlyCost=_round2(cost),
                pctOfTotal=(cost / total_spend) if total_spend else 0.0,
                forecastNextMonth=_round2(cost * (1 + growth_rate)) if growth_rate is not None else None,
            )
            for value, cost in sorted(values.items(), key=lambda item: (-item[1], item[0].lower()))
            if cost != 0 and value != "Unallocated"
        ]
        if not rows:
            continue
        dimensions.append(
            TagDimensionCost(
                tagKey=tag_key,
                unallocatedCost=_round2(values.get("Unallocated", 0.0)),
                rows=rows,
            )
        )
    return TagCostSummary(
        available=bool(dimensions),
        status=(
            "Cost grouped by each FOCUS tag key found (case-insensitive), inherited from "
            "the resource group when a resource has no tag of its own; still-untagged "
            "spend remains Unallocated per dimension."
            if dimensions
            else "No resource or resource-group tags were present."
        ),
        totalSpend=_round2(total_spend),
        growthRate=growth_rate,
        dimensions=dimensions,
    )


def _compliance_summary(
    subscription_ids: list[str],
    subscription_names: dict[str, str],
    untagged_counts: dict[str, dict[str, int]],
    policy_rows: list[dict],
) -> ComplianceSummary:
    policy_by_subscription = {str(row.get("subscriptionId") or "").lower(): row for row in policy_rows}
    rows = []
    for subscription_id in subscription_ids:
        normalized = subscription_id.lower()
        tags = untagged_counts.get(subscription_id) or untagged_counts.get(normalized) or {}
        total_resources = int(tags.get("total") or 0)
        untagged_resources = int(tags.get("untagged") or 0)
        policy = policy_by_subscription.get(normalized)
        if policy:
            compliant = int(policy.get("compliantEvaluations") or 0)
            non_compliant = int(policy.get("nonCompliantEvaluations") or 0)
            conflicts = int(policy.get("conflictEvaluations") or 0)
            exempt = int(policy.get("exemptEvaluations") or 0)
            not_started = int(policy.get("notStartedEvaluations") or 0)
            denominator = compliant + non_compliant + conflicts
            evaluation_percentage = compliant / denominator if denominator else None
            status = "Policy evaluation data available."
        else:
            compliant = non_compliant = conflicts = exempt = not_started = 0
            evaluation_percentage = None
            status = "No Azure Policy state records were returned for this subscription."
        rows.append(ComplianceRow(
            subscription_id=subscription_id,
            subscription_name=subscription_names.get(subscription_id, subscription_id),
            total_resources=total_resources,
            untagged_resources=untagged_resources,
            tagging_percentage=((total_resources - untagged_resources) / total_resources) if total_resources else 0,
            compliant_evaluations=compliant,
            non_compliant_evaluations=non_compliant,
            conflict_evaluations=conflicts,
            exempt_evaluations=exempt,
            not_started_evaluations=not_started,
            non_compliant_resources=int(policy.get("nonCompliantResources") or 0) if policy else 0,
            policy_assignment_count=int(policy.get("policyAssignmentCount") or 0) if policy else 0,
            evaluation_compliance_percentage=evaluation_percentage,
            policy_data_available=policy is not None,
            status=status,
        ))
    return ComplianceSummary(
        available=any(row.policy_data_available for row in rows),
        status=(
            "Tagging is resource-based; policy percentages use current policy evaluation records and may include multiple evaluations per resource."
            if rows
            else "No compliance scope was selected."
        ),
        rows=rows,
    )


def _domain_summary(
    domain: str,
    categories: list[FindingCategorySummary],
    service_family_spend: dict[str, float],
    service_spend: dict[str, float],
    total_spend: float,
    advisor_recommendations: list[dict],
    cost_by_resource_id: dict[str, float],
    subscription_names: dict[str, str],
) -> DomainSummary:
    domain_categories = [c for c in categories if CATEGORY_DOMAIN.get(c.category) == domain]
    verified_saving_month = sum(
        c.monthly_total for c in domain_categories if c.impact_type == "potential_savings"
    )
    monthly_cost_at_risk = sum(
        c.monthly_total for c in domain_categories if c.impact_type == "cost_at_risk"
    )
    if domain in DOMAIN_CONSUMED_SERVICES:
        domain_spend = sum(
            spend
            for service, spend in service_spend.items()
            if service.lower() in DOMAIN_CONSUMED_SERVICES[domain]
        )
    else:
        domain_spend = service_family_spend.get(DOMAIN_SERVICE_FAMILY[domain], 0.0)
    return DomainSummary(
        domain=domain,
        domain_spend_month=_round2(domain_spend),
        verified_saving_month=_round2(verified_saving_month),
        verified_saving_year=_round2(verified_saving_month * 12),
        monthly_cost_at_risk=_round2(monthly_cost_at_risk),
        pct_of_domain_spend=(verified_saving_month / domain_spend) if domain_spend else 0.0,
        pct_of_total_spend=(domain_spend / total_spend) if total_spend else 0.0,
        categories=domain_categories,
        advisor_recommendations=_domain_advisor_recommendations(
            domain, advisor_recommendations, cost_by_resource_id, subscription_names
        ),
    )


def _resource_domain(resource_id: str) -> str | None:
    resource_id = resource_id.lower()
    if "/providers/microsoft.sql/" in resource_id or "/providers/microsoft.sqlvirtualmachine/" in resource_id:
        return "sql"
    if any(
        provider in resource_id
        for provider in (
            "/providers/microsoft.cognitiveservices/",
            "/providers/microsoft.machinelearningservices/",
            "/providers/microsoft.search/",
        )
    ):
        return "ai"
    return None


def _optional_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _subscription_id_from_resource_id(resource_id: str) -> str:
    match = re.match(r"^/subscriptions/([^/]+)", resource_id, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _advisor_recommendation(
    item: dict,
    cost_by_resource_id: dict[str, float],
    subscription_names: dict[str, str],
) -> DomainAdvisorRecommendation | None:
    props = item.get("properties") or {}
    resource_id = str((props.get("resourceMetadata") or {}).get("resourceId") or "")
    if not resource_id:
        return None
    short = props.get("shortDescription") or {}
    extended = props.get("extendedProperties") or {}
    annual = _optional_float(extended.get("annualSavingsAmount"))
    reported = _optional_float(extended.get("savingsAmount"))
    subscription_id = _subscription_id_from_resource_id(resource_id)
    if not subscription_id and len(subscription_names) == 1:
        subscription_id = next(iter(subscription_names))
    return DomainAdvisorRecommendation(
        resource_id=resource_id,
        resource_name=resource_id.rstrip("/").split("/")[-1],
        subscription_id=subscription_id,
        subscription_name=subscription_names.get(subscription_id, subscription_id),
        problem=str(short.get("problem") or "Azure Advisor identified an optimization review."),
        recommendation=str(short.get("solution") or "Review this recommendation in Azure Advisor."),
        estimated_savings=annual if annual is not None else reported,
        savings_period="annual" if annual is not None else str(extended.get("savingsPeriod") or "as reported"),
        billed_cost=cost_by_resource_id.get(resource_id.lower()),
    )


def _domain_advisor_recommendations(
    domain: str,
    advisor_recommendations: list[dict],
    cost_by_resource_id: dict[str, float],
    subscription_names: dict[str, str],
) -> list[DomainAdvisorRecommendation]:
    recommendations: list[DomainAdvisorRecommendation] = []
    for item in advisor_recommendations:
        recommendation = _advisor_recommendation(item, cost_by_resource_id, subscription_names)
        resource_id = recommendation.resource_id if recommendation else ""
        if not resource_id or _resource_domain(resource_id) != domain:
            continue
        recommendations.append(recommendation)
    return sorted(recommendations, key=lambda recommendation: recommendation.resource_name.lower())


def _affected_subscriptions(category: FindingCategorySummary) -> list[SubscriptionReference]:
    subscriptions = {
        line.subscription_id: line.subscription_name
        for line in category.lines
        if line.subscription_id
    }
    return [
        SubscriptionReference(subscription_id=subscription_id, subscription_name=subscription_name)
        for subscription_id, subscription_name in sorted(
            subscriptions.items(), key=lambda item: (item[1].lower(), item[0].lower())
        )
    ]


def _subscription_breakdown(
    subscription_ids: list[str],
    subscription_names: dict[str, str],
    per_sub_spend: dict[str, float],
    categories: list[FindingCategorySummary],
) -> list[SubscriptionBreakdownRow]:
    rows = []
    for sub_id in subscription_ids:
        category_costs: dict[str, float] = {}
        total_waste = 0.0
        for cat in categories:
            cost = sum(line.monthly_cost or 0 for line in cat.lines if line.subscription_id == sub_id)
            category_costs[cat.category] = _round2(cost)
            if cat.impact_type == "potential_savings":
                total_waste += cost
        spend = per_sub_spend.get(sub_id, 0.0)
        rows.append(
            SubscriptionBreakdownRow(
                subscription_id=sub_id,
                subscription_name=subscription_names.get(sub_id, sub_id),
                current_spend=_round2(spend),
                category_costs=category_costs,
                total_waste=_round2(total_waste),
                pct_saved=(total_waste / spend) if spend else 0.0,
            )
        )
    return sorted(rows, key=lambda r: r.total_waste, reverse=True)


def _advisor_reconciliation(
    advisor_recommendations: list[dict],
    cost_by_resource_id: dict[str, float],
    currency: str,
    subscription_names: dict[str, str],
) -> AdvisorReconciliation:
    flagged: dict[str, float] = {}
    for rec in advisor_recommendations:
        props = rec.get("properties") or {}
        resource_id = str((props.get("resourceMetadata") or {}).get("resourceId", "")).lower()
        if not resource_id:
            continue
        ext = props.get("extendedProperties") or {}
        raw = ext.get("annualSavingsAmount") or ext.get("savingsAmount")
        try:
            amount = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        flagged[resource_id] = flagged.get(resource_id, 0.0) + amount

    claimed_total = sum(flagged.values())
    billed_total = sum(cost_by_resource_id.get(rid, 0.0) for rid in flagged)
    no_billing_record = sum(1 for rid in flagged if rid not in cost_by_resource_id)

    recommendations = [
        recommendation
        for item in advisor_recommendations
        if (recommendation := _advisor_recommendation(item, cost_by_resource_id, subscription_names))
    ]
    return AdvisorReconciliation(
        measures=[
            AdvisorMeasure(
                measure="Advisor cost recommendations in scope",
                value=str(len(flagged)),
                explanation="Distinct resources flagged by Azure Advisor's Cost category for the selected subscriptions.",
            ),
            AdvisorMeasure(
                measure="Advisor claimed saving (as reported)",
                value=f"{currency} {claimed_total:,.2f}",
                monetary_value=claimed_total,
                explanation=(
                    "Sum of Advisor's own reported savings field per recommendation; some recommendations "
                    "report annual figures, others monthly - check the Advisor portal for the exact period per item."
                ),
            ),
            AdvisorMeasure(
                measure="Closed-period effective cost of flagged resources",
                value=f"{currency} {billed_total:,.2f}",
                monetary_value=billed_total,
                explanation="FOCUS EffectiveCost for these flagged resources in the closed period - not Advisor's estimate.",
            ),
            AdvisorMeasure(
                measure="Flagged resources with no billing record",
                value=f"{no_billing_record} of {len(flagged)}",
                explanation="These resources do not appear in this month's Cost Management export, so no saving can be verified for them.",
            ),
        ],
        recommendations=sorted(
            recommendations,
            key=lambda item: (item.subscription_name.lower(), item.resource_name.lower()),
        ),
    )


def _governance(
    untagged_counts: dict[str, dict[str, int]], subscription_names: dict[str, str]
) -> list[GovernanceRow]:
    # "% of estate" = this subscription's share of the TOTAL untagged count across all
    # selected subscriptions (matches the original Excel: 376/1265 untagged = 29.7%),
    # not this subscription's share of all resources.
    total_untagged = sum(bucket["untagged"] for bucket in untagged_counts.values())
    rows = [
        GovernanceRow(
            subscription_id=sub_id,
            subscription_name=subscription_names.get(sub_id, sub_id),
            untagged_resources=bucket["untagged"],
            pct_of_estate=(bucket["untagged"] / total_untagged) if total_untagged else 0.0,
        )
        for sub_id, bucket in untagged_counts.items()
    ]
    return sorted(rows, key=lambda r: r.untagged_resources, reverse=True)


def _operational_signals(resource_graph_rows: dict[str, list[dict]]) -> list[OperationalSignal]:
    old_snapshots = resource_graph_rows.get("old_snapshots", [])
    stopped_vms = resource_graph_rows.get("stopped_vms", [])
    unattached_disks = resource_graph_rows.get("unattached_disks", [])
    disk_tb = sum(float(row.get("sizeGb") or 0) for row in unattached_disks) / 1024
    return [
        OperationalSignal(
            key="old_snapshots",
            label="Snapshots >12 months",
            value=f"{len(old_snapshots):,}",
            detail="Current inventory candidates reconciled to the closed billing period.",
            tone="critical" if old_snapshots else "neutral",
        ),
        OperationalSignal(
            key="stopped_vms",
            label="Stopped, not deallocated",
            value=f"{len(stopped_vms):,}",
            detail="VMs in a stopped state that can continue to incur compute cost.",
            tone="critical" if stopped_vms else "neutral",
        ),
        OperationalSignal(
            key="unattached_disks",
            label="Unattached managed disks",
            value=f"{len(unattached_disks):,}",
            detail=f"{disk_tb:,.1f} TB currently unattached and subject to owner validation.",
            tone="warning" if unattached_disks else "neutral",
        ),
    ]


def _pricing_summary(
    subscription_ids: list[str],
    subscription_names: dict[str, str],
    focus_data: FocusCostData | None,
    reconciliation: FocusReconciliation | None,
    unavailable_reason: str,
) -> PricingSummary:
    if focus_data is None or (reconciliation is not None and not reconciliation.reconciled):
        return PricingSummary(
            available=False,
            status=unavailable_reason,
            data_version=None,
            period=None,
            billing_currency=None,
            pricing_currencies=[],
            billed_cost=0,
            effective_cost=0,
            list_cost=0,
            contracted_cost=0,
            negotiated_discount=0,
            negotiated_discount_percentage=0,
            reconciliation_variance=None,
            subscriptions=[],
        )

    subscriptions = []
    for subscription_id in subscription_ids:
        list_cost = focus_data.list_cost_by_subscription.get(subscription_id, 0.0)
        negotiated_discount = focus_data.negotiated_discount_by_subscription.get(subscription_id, 0.0)
        subscriptions.append(
            PricingSubscriptionSummary(
                subscription_id=subscription_id,
                subscription_name=subscription_names.get(
                    subscription_id,
                    focus_data.subscription_names.get(subscription_id, subscription_id),
                ),
                billed_cost=focus_data.billed_cost_by_subscription.get(subscription_id, 0.0),
                effective_cost=focus_data.effective_cost_by_subscription.get(subscription_id, 0.0),
                list_cost=list_cost,
                contracted_cost=focus_data.contracted_cost_by_subscription.get(subscription_id, 0.0),
                negotiated_discount=negotiated_discount,
                negotiated_discount_percentage=(negotiated_discount / list_cost) if list_cost else 0.0,
            )
        )
    list_total = sum(item.list_cost for item in subscriptions)
    discount_total = sum(item.negotiated_discount for item in subscriptions)
    return PricingSummary(
        available=True,
        status="FOCUS pricing evidence",
        data_version=focus_data.data_version,
        period=focus_data.period,
        billing_currency=focus_data.currency,
        pricing_currencies=focus_data.pricing_currencies,
        billed_cost=sum(item.billed_cost for item in subscriptions),
        effective_cost=sum(item.effective_cost for item in subscriptions),
        list_cost=list_total,
        contracted_cost=sum(item.contracted_cost for item in subscriptions),
        negotiated_discount=discount_total,
        negotiated_discount_percentage=(discount_total / list_total) if list_total else 0.0,
        reconciliation_variance=reconciliation.variance if reconciliation else None,
        subscriptions=subscriptions,
    )


def _commitment_summary(focus_data: FocusCostData | None) -> CommitmentSummary:
    if focus_data is None:
        empty = CommitmentBenefitBreakdown(
            row_count=0,
            used_effective_cost=0,
            unused_effective_cost=0,
            realized_benefit=0,
        )
        return CommitmentSummary(
            observed=False,
            status="A complete FOCUS period is not available.",
            period="",
            currency="",
            reservations=empty,
            savings_plans=empty,
        )

    reservations = CommitmentBenefitBreakdown(**asdict(focus_data.reservation_commitment))
    savings_plans = CommitmentBenefitBreakdown(**asdict(focus_data.savings_plan_commitment))
    observed = reservations.row_count > 0 or savings_plans.row_count > 0
    return CommitmentSummary(
        observed=observed,
        status=(
            "Verified FOCUS commitment activity."
            if observed
            else "No reservation or savings-plan commitment rows were present in this FOCUS period."
        ),
        period=focus_data.period,
        currency=focus_data.currency,
        reservations=reservations,
        savings_plans=savings_plans,
    )


def _is_compute_vm(resource_type: str) -> bool:
    return resource_type.strip().lower().rstrip("/") == "microsoft.compute/virtualmachines"


# Mon-Fri 08:00-19:00 counted as business hours (55 of 168 h/week); everything else -
# nights and weekends - is "off hours" for the shutdown-savings estimate below.
OFF_HOURS_FRACTION = round(113 / 168, 4)


def _extended_support_summary(focus_data: FocusCostData | None) -> ExtendedSupportSummary:
    """Resources billed for Azure Extended Security Updates, identified from the FOCUS
    ServiceName/meter category/subcategory columns (see services/focus_cost_reader.py's
    _is_extended_support_charge) - a real recurring charge, not an idle-resource guess."""
    if not focus_data or not focus_data.extended_support_costs:
        return ExtendedSupportSummary(
            available=False,
            status="No Extended Security Updates charges were found in the FOCUS cost data.",
            totalMonthlyCost=0,
            rows=[],
        )
    rows = sorted(
        (
            ExtendedSupportRow(
                subscriptionId=item.subscription_id,
                subscriptionName=item.subscription_name,
                resourceGroup=item.resource_group,
                resourceId=item.resource_id,
                resourceName=item.resource_name,
                resourceType=item.resource_type,
                monthlyCost=_round2(item.effective_cost),
            )
            for item in focus_data.extended_support_costs
            if item.effective_cost > 0
        ),
        key=lambda row: -row.monthly_cost,
    )
    return ExtendedSupportSummary(
        available=bool(rows),
        status=(
            "Billed for Azure Extended Security Updates (ESU); upgrading the OS or SQL "
            "Server version removes this recurring charge."
            if rows
            else "No Extended Security Updates charges were found in the FOCUS cost data."
        ),
        totalMonthlyCost=_round2(sum(row.monthly_cost for row in rows)),
        rows=rows,
    )


def _off_hours_savings_summary(focus_data: FocusCostData | None) -> OffHoursSavingsSummary:
    """Estimated saving if each running compute VM were stopped/deallocated outside a
    standard business-hours schedule. FOCUS data has no historical VM power-state or CPU
    signal, so this is a scheduling estimate against current spend - not a verified-idle
    finding; the status text says so explicitly."""
    if not focus_data or not focus_data.resource_costs:
        return OffHoursSavingsSummary(
            available=False,
            status="FOCUS resource cost detail was not collected.",
            offHoursFraction=OFF_HOURS_FRACTION,
            totalMonthlyCost=0,
            totalEstimatedSaving=0,
            rows=[],
        )
    rows = sorted(
        (
            OffHoursSavingsRow(
                subscriptionId=item.subscription_id,
                subscriptionName=item.subscription_name,
                resourceGroup=item.resource_group,
                resourceId=item.resource_id,
                resourceName=item.resource_name,
                monthlyCost=_round2(item.effective_cost),
                estimatedMonthlySaving=_round2(item.effective_cost * OFF_HOURS_FRACTION),
            )
            for item in focus_data.resource_costs
            if _is_compute_vm(item.resource_type) and item.effective_cost > 0
        ),
        key=lambda row: -row.estimated_monthly_saving,
    )
    return OffHoursSavingsSummary(
        available=bool(rows),
        status=(
            "Estimated saving if each VM is stopped/deallocated outside a standard "
            "Mon-Fri 08:00-19:00 business-hours schedule; based on current spend, not "
            "measured utilization - confirm actual usage before scheduling shutdowns."
            if rows
            else "No running virtual machines were found in the FOCUS cost data."
        ),
        offHoursFraction=OFF_HOURS_FRACTION,
        totalMonthlyCost=_round2(sum(row.monthly_cost for row in rows)),
        totalEstimatedSaving=_round2(sum(row.estimated_monthly_saving for row in rows)),
        rows=rows,
    )


def _safe_pct(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _commitment_insights(history: FocusHistoryData | None) -> CommitmentInsights:
    """Month-on-month reservation/savings-plan/Spot economics from FOCUS history rows only.

    Never mixes in Advisor estimates or Azure rate-recommendation projections - every value
    here reconciles to FOCUS EffectiveCost/ListCost/ContractedCost for `Usage` rows.
    """
    if history is None or not history.records:
        return CommitmentInsights(
            available=False,
            status="FOCUS history is not available for commitment insights.",
            months=[],
        )

    def _empty_bucket() -> dict:
        return {
            "reservation_used": 0.0,
            "reservation_unused": 0.0,
            "reservation_benefit": 0.0,
            "reservation_eligible_types": set(),
            "savings_plan_used": 0.0,
            "savings_plan_unused": 0.0,
            "savings_plan_benefit": 0.0,
            "acd_effective": 0.0,
            "acd_list": 0.0,
            "spot_effective": 0.0,
            "spot_list": 0.0,
            "vm_total": 0.0,
            "vm_payg": 0.0,
            "vm_savings_plan": 0.0,
            "vm_reservation": 0.0,
        }

    buckets: dict[str, dict] = {}
    for record in history.records:
        bucket = buckets.setdefault(record.date[:7], _empty_bucket())
        is_usage = record.charge_category.lower() == "usage"
        status = record.commitment_discount_status.lower()
        kind = _commitment_kind(record.commitment_discount_type)
        is_used = is_usage and status == "used"
        is_unused = status == "unused"

        if kind == "reservation":
            if is_used:
                bucket["reservation_used"] += record.effective_cost
                bucket["reservation_benefit"] += max(record.contracted_cost - record.effective_cost, 0.0)
                bucket["reservation_eligible_types"].add(record.resource_type)
            elif is_unused:
                bucket["reservation_unused"] += record.effective_cost
        elif kind == "savings_plan":
            if is_used:
                bucket["savings_plan_used"] += record.effective_cost
                bucket["savings_plan_benefit"] += max(record.contracted_cost - record.effective_cost, 0.0)
            elif is_unused:
                bucket["savings_plan_unused"] += record.effective_cost

        if is_used and kind in ("reservation", "savings_plan"):
            bucket["acd_effective"] += record.effective_cost
            bucket["acd_list"] += record.list_cost

        if is_usage and record.pricing_category.lower() == "spot":
            bucket["spot_effective"] += record.effective_cost
            bucket["spot_list"] += record.list_cost

        if is_usage and _is_compute_vm(record.resource_type):
            bucket["vm_total"] += record.effective_cost
            if kind == "savings_plan" and is_used:
                bucket["vm_savings_plan"] += record.effective_cost
            elif kind == "reservation" and is_used:
                bucket["vm_reservation"] += record.effective_cost
            elif status not in ("used", "unused"):
                bucket["vm_payg"] += record.effective_cost

    # Second pass: PAYG-eligible usage cost for resource types that had at least one
    # reservation-covered row in the same month - the denominator for org-level coverage.
    payg_eligible: dict[str, float] = {month: 0.0 for month in buckets}
    for record in history.records:
        month = record.date[:7]
        bucket = buckets[month]
        if not bucket["reservation_eligible_types"]:
            continue
        status = record.commitment_discount_status.lower()
        if (
            record.charge_category.lower() == "usage"
            and status not in ("used", "unused")
            and record.resource_type in bucket["reservation_eligible_types"]
        ):
            payg_eligible[month] += record.effective_cost

    points: list[CommitmentMonthPoint] = []
    for month in sorted(buckets):
        bucket = buckets[month]
        acd_savings = max(bucket["acd_list"] - bucket["acd_effective"], 0.0)
        spot_savings = max(bucket["spot_list"] - bucket["spot_effective"], 0.0)
        vm_total = bucket["vm_total"]
        reservation_coverage = (
            _safe_pct(bucket["reservation_used"], bucket["reservation_used"] + payg_eligible[month])
            if bucket["reservation_eligible_types"]
            else None
        )
        points.append(
            CommitmentMonthPoint(
                month=month,
                currency=history.currency,
                reservation_committed_cost=_round2(bucket["reservation_used"] + bucket["reservation_unused"]),
                reservation_used_cost=_round2(bucket["reservation_used"]),
                reservation_unused_cost=_round2(bucket["reservation_unused"]),
                reservation_realized_savings=_round2(bucket["reservation_benefit"]),
                reservation_coverage_percentage=reservation_coverage,
                savings_plan_committed_cost=_round2(bucket["savings_plan_used"] + bucket["savings_plan_unused"]),
                savings_plan_used_cost=_round2(bucket["savings_plan_used"]),
                savings_plan_unused_cost=_round2(bucket["savings_plan_unused"]),
                savings_plan_realized_savings=_round2(bucket["savings_plan_benefit"]),
                acd_effective_cost=_round2(bucket["acd_effective"]),
                acd_least_price_cost=_round2(bucket["acd_list"]),
                acd_savings=_round2(acd_savings),
                acd_savings_percentage=_safe_pct(acd_savings, bucket["acd_list"]),
                spot_effective_cost=_round2(bucket["spot_effective"]),
                spot_least_price_cost=_round2(bucket["spot_list"]),
                spot_savings=_round2(spot_savings),
                spot_savings_percentage=_safe_pct(spot_savings, bucket["spot_list"]),
                compute_vm_coverage=ComputeCoverageSplit(
                    total_cost=_round2(vm_total),
                    payg_cost=_round2(bucket["vm_payg"]),
                    savings_plan_cost=_round2(bucket["vm_savings_plan"]),
                    reservation_cost=_round2(bucket["vm_reservation"]),
                    payg_percentage=_safe_pct(bucket["vm_payg"], vm_total),
                    savings_plan_percentage=_safe_pct(bucket["vm_savings_plan"], vm_total),
                    reservation_percentage=_safe_pct(bucket["vm_reservation"], vm_total),
                ),
            )
        )

    return CommitmentInsights(
        available=True,
        status=f"{len(points)} FOCUS history month(s) available.",
        months=points,
    )


def _severity(monthly_saving: float, total_spend: float) -> str:
    ratio = monthly_saving / total_spend if total_spend else 0.0
    if ratio >= 0.05:
        return "Critical"
    if ratio >= 0.01:
        return "High"
    return "Medium"


def _prioritized_findings(
    categories: list[FindingCategorySummary],
    total_spend: float,
    untagged_counts: dict[str, dict[str, int]],
) -> list[ExecutiveFinding]:
    findings: list[ExecutiveFinding] = []
    for category in sorted(categories, key=lambda item: item.monthly_total, reverse=True):
        if category.impact_type == "inventory":
            continue
        affected_subscriptions = len({line.subscription_id for line in category.lines})
        findings.append(
            ExecutiveFinding(
                rank=0,
                category=category.category,
                finding=FINDING_HEADLINES.get(category.category, category.display_name),
                evidence=(
                    f"{category.count:,} resources across {affected_subscriptions} subscription(s) match the current "
                    f"inventory rule and reconcile to ${category.monthly_total:,.2f} in the closed-period FOCUS EffectiveCost export."
                    + (
                        " This is billed cost at risk pending owner validation, not confirmed saving."
                        if category.impact_type == "cost_at_risk"
                        else ""
                    )
                ),
                impact_type=category.impact_type,
                monthly_saving=category.monthly_total if category.impact_type == "potential_savings" else None,
                monthly_cost_at_risk=category.monthly_total if category.impact_type == "cost_at_risk" else None,
                severity=_severity(category.monthly_total, total_spend),
            )
        )

    total_untagged = sum(bucket["untagged"] for bucket in untagged_counts.values())
    if total_untagged:
        findings.append(
            ExecutiveFinding(
                rank=0,
                category="untagged_resources",
                finding=f"Tagging gaps affect {total_untagged:,} resources",
                evidence=(
                    "Untagged resources reduce allocation and showback quality. This governance finding carries no "
                    "fabricated savings value and should be remediated through policy and ownership controls."
                ),
                impact_type="cost_at_risk",
                monthly_saving=None,
                monthly_cost_at_risk=None,
                severity="High",
            )
        )

    ranked = findings[:6]
    return [finding.model_copy(update={"rank": index}) for index, finding in enumerate(ranked, start=1)]


def build_full_report(
    subscription_ids: list[str],
    subscription_names: dict[str, str],
    per_sub_spend: dict[str, float],
    resource_graph_rows: dict[str, list[dict]],
    cost_by_resource_id: dict[str, float],
    service_family_spend: dict[str, float],
    advisor_recommendations: list[dict],
    untagged_counts: dict[str, dict[str, int]],
    service_spend: dict[str, float] | None = None,
    cost_evidence_by_resource_id: dict[str, list[dict]] | None = None,
    network_metric_coverage: list[dict] | None = None,
    report_metadata: ReportMetadata | None = None,
    completeness: DataCompleteness | None = None,
    focus_cost_data: FocusCostData | None = None,
    focus_history: FocusHistoryData | None = None,
    advisor_score_rows: list[dict] | None = None,
    storage_accounts: list[dict] | None = None,
    storage_metrics: dict[str, dict] | None = None,
    ai_usage_data: dict | None = None,
    policy_compliance_rows: list[dict] | None = None,
    focus_reconciliation: FocusReconciliation | None = None,
    focus_unavailable_reason: str = "A complete reconciled FocusCost period is not available.",
) -> FullReport:
    total_monthly_spend = sum(per_sub_spend.values())
    base = build_report(
        subscription_ids,
        total_monthly_spend,
        resource_graph_rows,
        cost_by_resource_id,
        cost_evidence_by_resource_id,
        subscription_names,
        {
            resource_id: [asdict(item) for item in items]
            for resource_id, items in (focus_cost_data.pricing_evidence_by_resource_id.items() if focus_cost_data else [])
        },
    )
    categories = [
        category.model_copy(
            update={
                "remediation": (
                    build_remediation_plan(category.category, category.lines)
                    if category.category in REMEDIATION_DEFINITIONS
                    else None
                )
            }
        )
        for category in base.tier_a_categories
    ]

    savings_month = sum(c.monthly_total for c in categories if c.impact_type == "potential_savings")
    spend_history = _spend_history(focus_history, focus_cost_data)
    daily_cost_trend = _daily_cost_trend(focus_history)
    tag_daily_cost_trend = _tag_daily_cost_trend(focus_history)
    previous_spend = spend_history.months[-2].total if len(spend_history.months) > 1 else None
    idle_resource_ids = {
        line.resource_id.lower()
        for category in categories
        for line in category.lines
        if line.idle_evidence is not None and line.idle_evidence.classification == "confirmed_idle"
    }
    costed_resource_ids = {
        resource_id.lower()
        for resource_id, cost in (focus_cost_data.effective_cost_by_resource_id.items() if focus_cost_data else [])
        if cost > 0
    }
    review_resource_ids = {
        line.resource_id.lower() for category in categories for line in category.lines
        if line.idle_evidence is not None and line.idle_evidence.classification != "confirmed_idle"
    }
    idle_resources = len(idle_resource_ids)
    active_resources = len(costed_resource_ids - idle_resource_ids - review_resource_ids)
    wastage_month = savings_month + sum(
        category.monthly_total for category in categories if category.impact_type == "cost_at_risk"
    )
    executive_summary = ExecutiveSummary(
        current_monthly_spend=base.total_monthly_spend,
        potential_savings_month=_round2(savings_month),
        potential_savings_year=_round2(savings_month * 12),
        pct_recoverable=(savings_month / base.total_monthly_spend) if base.total_monthly_spend else 0.0,
        estimated_wastage_month=_round2(wastage_month),
        pct_wastage=(wastage_month / base.total_monthly_spend) if base.total_monthly_spend else 0.0,
        active_resources=active_resources,
        idle_resources=idle_resources,
        idle_review_candidates=len(review_resource_ids),
        idle_resource_percentage=(idle_resources / len(costed_resource_ids | idle_resource_ids | review_resource_ids)) if costed_resource_ids | idle_resource_ids | review_resource_ids else 0.0,
        spend_change_percentage=(
            (base.total_monthly_spend - previous_spend) / previous_spend
            if previous_spend and previous_spend > 0
            else None
        ),
    )

    domains = {
        domain: _domain_summary(
            domain,
            categories,
            service_family_spend,
            service_spend or {},
            base.total_monthly_spend,
            advisor_recommendations,
            cost_by_resource_id,
            subscription_names,
        )
        for domain in ("compute", "storage", "network", "sql", "ai")
    }

    savings_roadmap = sorted(
        (
            SavingsRoadmapItem(
                category=c.category,
                opportunity=c.display_name,
                domain=DOMAIN_DISPLAY_NAMES.get(CATEGORY_DOMAIN.get(c.category, ""), "Other"),
                monthly=c.monthly_total,
                annual=c.annual_total,
                resources=c.count,
                recommended_action=RECOMMENDED_ACTIONS.get(c.category, "Review and remediate."),
                impact_type=c.impact_type,
                immediate=bool(c.remediation and c.remediation.immediate),
                risk=c.remediation.risk if c.remediation else "High",
                effort=c.remediation.effort if c.remediation else "High",
                prerequisites=c.remediation.prerequisites if c.remediation else [],
                affected_subscriptions=_affected_subscriptions(c),
            )
            for c in categories
            if c.impact_type != "inventory"
        ),
        key=lambda item: (not item.immediate, -item.monthly, item.opportunity),
    )

    action_plan = sorted(
        (
            ActionPlanItem(
                action_id=c.category,
                action=ACTION_PLAN_VERBS.get(c.category, c.display_name).format(count=c.count),
                saving_month=c.monthly_total,
                prerequisite=RECOMMENDED_ACTIONS.get(c.category, "Review and remediate."),
                affected_subscriptions=_affected_subscriptions(c),
            )
            for c in categories
            if c.impact_type == "potential_savings"
        ),
        key=lambda item: item.saving_month,
        reverse=True,
    )

    subscription_breakdown = _subscription_breakdown(
        subscription_ids, subscription_names, per_sub_spend, categories
    )
    metadata = report_metadata or ReportMetadata(
        period="",
        period_start="",
        period_end="",
        cost_basis="ActualCost",
        currency="USD",
        generated_at="",
        source="Azure Cost Management Query API",
    )
    data_completeness = completeness or DataCompleteness(
        requested_subscriptions=len(subscription_ids),
        available_subscriptions=len(per_sub_spend),
        complete=len(per_sub_spend) == len(subscription_ids),
        status="Complete" if len(per_sub_spend) == len(subscription_ids) else "Incomplete",
    )
    advisor_reconciliation = _advisor_reconciliation(
        advisor_recommendations, cost_by_resource_id, metadata.currency, subscription_names
    )
    governance = _governance(untagged_counts, subscription_names)

    return FullReport(
        executive_summary=executive_summary,
        report_metadata=metadata,
        completeness=data_completeness,
        operational_signals=_operational_signals(resource_graph_rows),
        top_services=_top_services(service_spend or {}, base.total_monthly_spend),
        spend_categories=_spend_categories(
            focus_cost_data.provider_spend if focus_cost_data else service_spend or {},
            base.total_monthly_spend,
        ),
        spend_history=spend_history,
        daily_cost_trend=daily_cost_trend,
        tag_daily_cost_trend=tag_daily_cost_trend,
        cost_details=build_cost_details(focus_history),
        cost_hierarchy=_cost_hierarchy(focus_cost_data, base.total_monthly_spend),
        region_spend=_region_spend(focus_cost_data, base.total_monthly_spend),
        advisor_score=_advisor_score(advisor_score_rows or []),
        storage_optimization=_storage_optimization(
            storage_accounts or [],
            storage_metrics or {},
            subscription_names,
            focus_cost_data,
        ),
        ai_usage=_ai_usage_summary(ai_usage_data),
        chargeback=_chargeback_summary(focus_cost_data, base.total_monthly_spend),
        compliance=_compliance_summary(
            subscription_ids,
            subscription_names,
            untagged_counts,
            policy_compliance_rows or [],
        ),
        network_metric_coverage=network_metric_coverage or [],
        prioritized_findings=_prioritized_findings(categories, base.total_monthly_spend, untagged_counts),
        savings_roadmap=savings_roadmap,
        subscription_breakdown=subscription_breakdown,
        domains=domains,
        action_plan=action_plan,
        advisor_reconciliation=advisor_reconciliation,
        governance=governance,
        pricing_summary=_pricing_summary(
            subscription_ids,
            subscription_names,
            focus_cost_data,
            focus_reconciliation,
            focus_unavailable_reason,
        ),
        commitment_summary=_commitment_summary(focus_cost_data),
        commitment_insights=_commitment_insights(focus_history),
        tag_costs=_tag_cost_summary(
            focus_cost_data,
            base.total_monthly_spend,
            executive_summary.spend_change_percentage,
        ),
        extended_support=_extended_support_summary(focus_cost_data),
        off_hours_savings=_off_hours_savings_summary(focus_cost_data),
        tier_a_categories=categories,
    )
