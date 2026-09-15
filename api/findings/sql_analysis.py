from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from .models import SqlOptimizationCheck, SqlResourceContext, SqlWorkloadEvidence


CONFIGURATION_FIELDS = (
    "skuName", "skuTier", "skuFamily", "capacity", "licenseType", "sqlServerLicenseType",
    "maxSizeBytes", "storageSizeInGB", "currentBackupStorageRedundancy", "requestedBackupStorageRedundancy",
    "zoneRedundant", "readScale", "computeModel", "autoPauseDelay", "minCapacity", "state", "imageSku",
)


def analyze_sql_resource(context: SqlResourceContext, row: dict) -> list[SqlOptimizationCheck]:
    model = context.deployment_model
    if model in {"unknown", "logical_server"}:
        return []
    context.configuration = {
        key: str(row[key]) for key in CONFIGURATION_FIELDS
        if key in row and row[key] is not None and isinstance(row[key], (str, int, float, bool)) and str(row[key]).strip()
    }
    checks = [
        SqlOptimizationCheck(
            ruleId="SQL-R01", title="Right-size capacity",
            reason="Inventory does not establish spare capacity or a safe target size.",
            requiredEvidence=["representative_30_90_day_utilization", "peak_concurrency", "memory_io_log_limits", "supported_target_sku", "workload_sla"],
            nextSteps=["Collect time-aligned CPU/DTU, memory, I/O, log and session peaks for a representative 30-90-day window.",
                       "Compare supported target limits with peak demand plus agreed headroom; benchmark before a maintenance-window change."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R02", title="Assess serverless suitability",
            reason="Intermittent demand and auto-pause eligibility have not been verified.",
            requiredEvidence=["active_idle_hours", "serverless_feature_eligibility", "resume_latency_budget", "compute_memory_pricing"],
            nextSteps=["Verify tier/features support serverless and identify sessions or jobs preventing auto-pause.",
                       "Price minimum/maximum capacity, memory and observed active hours; test resume latency before choosing pause settings."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R03", title="Review pool placement and sizing",
            reason="A pool proposal requires combined demand and compatible members, not average CPU alone.",
            requiredEvidence=["complete_pool_membership", "time_aligned_member_peaks", "pool_limits", "placement_compatibility"],
            nextSteps=["Inventory compatible database members and align their busy periods.",
                       "Size for the combined peak and per-database limits; compare pool and member costs without counting shared capacity twice."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R04", title="Review Azure Hybrid Benefit",
            reason="License configuration or entitlement is unverified.",
            requiredEvidence=["license_entitlement", "eligible_core_allocation", "purchasing_model", "license_component_cost"],
            nextSteps=["Have the licensing owner verify qualifying license/subscription rights and available core allocation.",
                       "Confirm deployment eligibility and price only the removable license component before changing license settings."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R05", title="Review storage and backup settings",
            reason="Configured capacity and redundancy do not establish avoidable storage or backup costs.",
            requiredEvidence=["used_storage_and_growth", "backup_pitr_ltr_policy", "recovery_compliance_approval", "supported_storage_change"],
            nextSteps=["Compare configured storage with used space and growth; inspect PITR/LTR, redundancy, RPO/RTO and geo-restore requirements.",
                       "Verify supported size increments/reduction paths and obtain recovery/compliance approval before any setting change."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R06", title="Review inactive copies and replicas",
            reason="No complete 30-day connection/activity history or owner intent has been established.",
            requiredEvidence=["complete_30_day_activity", "resource_owner_intent", "replica_dependencies", "retention_and_protection"],
            nextSteps=["Identify restore copies and secondaries; collect complete connection/activity evidence and check infrequent jobs.",
                       "Confirm owner, failover/DR dependencies, locks and retention; preserve required replicas and validate recovery before proposing retirement."],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R07", title="Evaluate reservations after optimization",
            reason="Commit only to a stable, eligible post-optimization baseline.",
            requiredEvidence=["post_optimization_baseline", "existing_commitments", "reservation_eligibility", "one_three_year_pricing"],
            nextSteps=["Complete chosen capacity, pool, tier and licensing changes; observe the remaining steady workload for a month.",
                       "Compare one- and three-year eligible reservation coverage with existing commitments and utilization risk; obtain purchasing approval."],
            dependsOn=[f"SQL-R{index:02d}" for index in (1, 2, 3, 4, 5, 6, 8)],
        ),
        SqlOptimizationCheck(
            ruleId="SQL-R08", title="Assess Business Critical to General Purpose",
            reason="Current tier and premium feature dependencies must be verified.",
            requiredEvidence=["premium_feature_dependencies", "io_latency_requirements", "availability_sla", "supported_tier_migration"],
            nextSteps=["Check in-memory features, read scale, replicas, latency/IOPS and business availability requirements.",
                       "Validate a supported General Purpose target with workload tests, a maintenance plan, current pricing and a recovery path."],
        ),
    ]
    by_rule = {check.rule_id: check for check in checks}
    tier = str(row.get("skuTier") or "").strip().casefold()
    license_type = str(row.get("sqlServerLicenseType") if model == "sql_vm" else row.get("licenseType") or "").casefold()
    if model != "single_database":
        by_rule["SQL-R02"].status = "not_applicable"
        by_rule["SQL-R02"].reason = "Single-database serverless migration is not a direct change for this deployment model."
    elif str(row.get("computeModel") or "").casefold() == "serverless":
        by_rule["SQL-R02"].status = "not_applicable"
        by_rule["SQL-R02"].reason = "The reported compute model is already serverless; review its limits and pause behavior separately."
    if model in {"managed_instance", "instance_pool"}:
        by_rule["SQL-R03"].title = "Assess Managed Instance pool placement"
        by_rule["SQL-R03"].next_steps = [
            "Verify compatible instance configurations, regional pool support and current instance-pool membership.",
            "Compare concurrent instance peaks, permitted vCore/storage increments and supported migration paths before proposing a pool.",
        ]
    elif model == "elastic_pool":
        by_rule["SQL-R01"].reason = "Size pool capacity against all member peaks, not one database's average utilization."
        by_rule["SQL-R03"].next_steps.insert(0, "Verify the current pool membership before flagging an orphan pool; missing members are not an empty pool.")
    elif model == "pooled_database":
        by_rule["SQL-R01"].reason = "This database shares pool capacity; review its per-database limits together with the parent pool."
    elif model == "sql_vm":
        by_rule["SQL-R01"].next_steps = [
            "Join the SQL registration to its compute VM and inspect VM CPU/memory, disk IOPS/throughput and SQL workload peaks.",
            "Compare eligible VM SKUs, constrained-core/edition licensing and disk limits; validate an owner-approved shutdown schedule for intermittent workloads.",
        ]
        by_rule["SQL-R03"].status = "not_applicable"
        by_rule["SQL-R03"].reason = "PaaS elastic/instance pools are not an in-place SQL VM optimization."
    if tier in {"basic", "standard", "premium"} and model in {"single_database", "pooled_database", "elastic_pool"}:
        by_rule["SQL-R04"].status = "not_applicable"
        by_rule["SQL-R04"].reason = "The reported DTU tier does not support the vCore Azure Hybrid Benefit setting."
    elif license_type in {"baseprice", "ahub"}:
        by_rule["SQL-R04"].status = "not_applicable"
        by_rule["SQL-R04"].reason = "Azure Hybrid Benefit is already configured; this is not proof that license entitlement is valid."
    elif license_type in {"licenseincluded", "payg"}:
        by_rule["SQL-R04"].status = "review"
        by_rule["SQL-R04"].reason = "License-included billing is reported; eligible customer licenses may remove the license component after entitlement verification."
    if model == "sql_vm":
        by_rule["SQL-R08"].status = "not_applicable"
        by_rule["SQL-R08"].reason = "Business Critical and General Purpose are PaaS tiers, not SQL VM editions."
    elif tier in {"businesscritical", "business critical"}:
        by_rule["SQL-R08"].status = "review"
        by_rule["SQL-R08"].reason = "Business Critical is reported; confirm premium features and workload SLA before considering General Purpose."
    elif tier in {"generalpurpose", "general purpose", "hyperscale", "basic", "standard", "premium"}:
        by_rule["SQL-R08"].status = "not_applicable"
        by_rule["SQL-R08"].reason = "The reported resource is not on the Business Critical tier."
    if context.evidence_gaps:
        for check in checks:
            if check.status != "not_applicable":
                check.required_evidence = list(dict.fromkeys([*context.evidence_gaps, *check.required_evidence]))
    raw_evidence = row.get("sqlWorkloadEvidence")
    if raw_evidence is not None:
        try:
            context.workload_evidence = SqlWorkloadEvidence.model_validate(raw_evidence)
        except ValidationError:
            context.evidence_gaps.append("workload_evidence_invalid")
    evidence = context.workload_evidence
    if evidence is not None:
        resource_id = context.compute_resource_id if model == "sql_vm" else row.get("id")
        try:
            start, end, collected = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (evidence.window_start, evidence.window_end, evidence.collected_at)]
            now = datetime.now(timezone.utc)
            valid = (all(value.tzinfo is not None for value in (start, end, collected))
                     and end - start == timedelta(days=30) and timedelta(0) <= now - end <= timedelta(days=2)
                     and end <= collected <= now + timedelta(minutes=5)
                     and evidence.resource_id.casefold() == str(resource_id or "").casefold())
        except (ValueError, TypeError):
            valid = False
        if evidence.status == "complete" and valid:
            metrics = {metric.name: metric for metric in evidence.metrics if metric.observed_days == 30 and metric.maximum is not None}
            required = {"Percentage CPU"} if model == "sql_vm" else {"avg_cpu_percent", "avg_workers_percent"} if model == "managed_instance" else {"cpu_percent", "physical_data_read_percent", "log_write_percent", "sessions_percent", "workers_percent"}
            if set(metrics) == required and len(metrics) == len(evidence.metrics) and evidence.expected_days == 30:
                peak = max(metric.maximum for metric in metrics.values())
                if peak >= 80:
                    by_rule["SQL-R01"].status = "blocked"
                    by_rule["SQL-R01"].reason = f"A collected utilization metric reached {peak:g}% in the observed window; a lower capacity needs workload validation."
                elif peak <= 50:
                    by_rule["SQL-R01"].status = "review"
                    by_rule["SQL-R01"].reason = f"Collected daily maxima stayed at or below {peak:g}% across 30 UTC days. Review remaining memory/I/O, seasonality and target limits; no safe target or saving is established."
                sessions = metrics.get("sessions_percent")
                if sessions and sessions.maximum > 0:
                    by_rule["SQL-R06"].status = "blocked"
                    by_rule["SQL-R06"].reason = "Sessions were observed within the 30-day window; this is not a zero-use database."
        elif evidence.status != "complete":
            by_rule["SQL-R01"].reason = f"Workload evidence is {evidence.status}: {evidence.reason}"
    tags = row.get("tags")
    if isinstance(tags, dict) and any(str(key).casefold() in {"donotdelete", "protected"} and str(value).casefold() in {"true", "yes", "1"} for key, value in tags.items()):
        by_rule["SQL-R06"].status = "blocked"
        by_rule["SQL-R06"].reason = "A protective resource tag prevents a retirement recommendation; preserve the resource and confirm owner policy."
    return checks