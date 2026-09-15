import math
from datetime import date, datetime, timedelta, timezone

from .models import IdleEvidence


IDLE_CATEGORIES = {
    "unattached_disks", "stopped_vms", "idle_public_ips", "empty_backend_pools", "empty_load_balancer_backend_pools",
    "idle_virtual_network_gateways", "idle_nat_gateways", "idle_expressroute_circuits", "old_snapshots",
    "unattached_network_interfaces", "unassociated_network_security_groups", "unassociated_route_tables",
    "empty_availability_sets", "deallocated_virtual_machines", "zero_instance_vm_scale_sets", "empty_app_service_plans",
    "stopped_web_apps", "empty_virtual_networks", "disconnected_private_endpoints", "stopped_aks_clusters",
    "empty_resource_groups", "old_custom_images",
}
_NETWORK_CATEGORIES = {"idle_virtual_network_gateways", "idle_nat_gateways", "idle_expressroute_circuits"}


def assess_idle_evidence(category: str, row: dict) -> IdleEvidence | None:
    if category not in IDLE_CATEGORIES:
        return None
    evidence = IdleEvidence(classification="candidate", reason="State/configuration only; activity, history and owner/dependency checks are incomplete.", source="Azure Resource Graph")
    tags = row.get("tags")
    if isinstance(tags, dict) and any(str(key).casefold() in {"donotdelete", "protected", "disasterrecovery", "standby"}
                                     and str(value).casefold() in {"true", "yes", "1"} for key, value in tags.items()):
        evidence.classification = "protected"
        evidence.reason = "Protection or standby context requires owner review; inactivity is not permission to remove this resource."
        return evidence
    if category in {"stopped_vms", "deallocated_virtual_machines"}:
        evidence.reason = "Current power state does not establish the start or continuity of the stopped interval; confirmed stop history and owner context are required."
    if category not in _NETWORK_CATEGORIES:
        return evidence
    evidence.source = "Azure Monitor Metrics / Resource Graph"
    evidence.window_start = row.get("metricPeriodStart")
    evidence.window_end = row.get("metricPeriodEnd")
    total = row.get("metricTotal")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total) and total > 0:
        evidence.classification = "activity_observed"
        evidence.reason = "Positive network traffic was observed; the resource is not zero-use in this window."
        return evidence
    try:
        start, end = date.fromisoformat(evidence.window_start or ""), date.fromisoformat(evidence.window_end or "")
        expected = (end - start).days + 1
        complete = (expected >= 30 and row.get("metricDays") == expected and row.get("metricNames")
                    and row.get("evidenceType") == "metrics_verified_idle" and total == 0 and not isinstance(total, bool))
        fresh = end == datetime.now(timezone.utc).date() - timedelta(days=1)
    except (ValueError, TypeError):
        complete = fresh = False
    if complete and fresh:
        evidence.classification = "zero_traffic_observed"
        evidence.reason = "Complete daily aggregates show zero traffic in the recent window; dependency, standby and owner checks are still required before confirming idle."
    else:
        evidence.reason = "Traffic evidence is incomplete, stale, or lacks a verified observation window; this remains a review candidate."
    return evidence