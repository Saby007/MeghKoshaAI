"""Publish FOCUS financial evidence independently of optional live Azure enrichment."""

import asyncio
import logging
from datetime import date
from uuid import UUID

import httpx
from azure.core.exceptions import AzureError

from anomalies.engine import detect_anomalies
from reports.builder import build_full_report
from reports.models import DataCompleteness, FullReport, ReportMetadata
from services import arm_client, manual_focus, sql_metrics, target_connection
from services.assessment_limits import AssessmentLimitError

logger = logging.getLogger(__name__)


async def build(dataset_id: UUID, subscription_ids: list[str], stale_days: int) -> tuple[FullReport, manual_focus.Evidence]:
    from main import (
        _ADVISOR_SCORE_QUERY, _METRIC_NETWORK_CATEGORIES, _POLICY_COMPLIANCE_QUERY,
        _PROTECTED_TAG_KEYS, _QUERIES, _STALE_CATEGORIES, _STORAGE_ACCOUNTS_QUERY,
        _exclude_protected_resources, _render_queries,
    )

    if len(subscription_ids) != 1:
        raise ValueError("An uploaded report must contain exactly one target subscription.")
    configuration = await target_connection.require_subscription(subscription_ids[0])
    evidence = await manual_focus.load(dataset_id, configuration.subscription_id)
    costs, history = evidence.costs, evidence.history
    names = {configuration.subscription_id: configuration.subscription_name}
    names.update(costs.subscription_names)
    graph_rows = {category: [] for category in _QUERIES}
    advisors = []
    untagged = {}
    advisor_score = []
    storage_accounts = []
    policy = []
    storage_metrics = {}
    ai_usage = {}
    network_coverage = []
    errors: list[str] = []
    enrichment = "not_configured"
    message = "Service-principal credentials were not provided. Financial views use the uploaded FOCUS report; live Azure sections are unavailable."

    async def optional(label, operation, empty):
        try:
            return await operation
        except AssessmentLimitError:
            raise
        except (AzureError, httpx.HTTPError, TimeoutError, ValueError):
            errors.append(label)
            logger.warning("Optional Azure evidence unavailable: %s", label)
            return empty

    if configuration.has_credentials:
        try:
            await target_connection.access_token()
        except (AzureError, httpx.HTTPError, TimeoutError):
            logger.warning("The saved target credential could not authenticate during assessment")
            enrichment = "failed"
            message = "The supplied service principal could not authenticate. Update its credentials in Admin. Uploaded financial evidence remains available."
        else:
            rendered = _render_queries(stale_days)
            graph_lists, advisors, untagged, advisor_score, storage_accounts, policy = await asyncio.gather(
                asyncio.gather(*(
                    optional(category, arm_client.query_resource_graph(query, subscription_ids), [])
                    for category, query in rendered.items()
                )),
                optional("Advisor", arm_client.list_advisor_recommendations(configuration.subscription_id), []),
                optional("Tag inventory", arm_client.get_untagged_resource_counts(subscription_ids), {}),
                optional("Advisor Score", arm_client.query_resource_graph(_ADVISOR_SCORE_QUERY, subscription_ids), []),
                optional("Storage inventory", arm_client.query_resource_graph(_STORAGE_ACCOUNTS_QUERY, subscription_ids), []),
                optional("Policy", arm_client.query_resource_graph(_POLICY_COMPLIANCE_QUERY, subscription_ids), []),
            )
            graph_rows = dict(zip(rendered, graph_lists))
            for category in _STALE_CATEGORIES:
                graph_rows[category], _ = _exclude_protected_resources(graph_rows.get(category, []))
            metric_results = await asyncio.gather(*(
                optional(category, arm_client.collect_zero_traffic_findings(
                    category, graph_rows.get(category, []), costs.period_start, costs.period_end,
                ), None)
                for category in _METRIC_NETWORK_CATEGORIES
            ))
            for category, result in zip(_METRIC_NETWORK_CATEGORIES, metric_results):
                if result is None:
                    graph_rows[category] = []
                    continue
                findings, coverage = result
                graph_rows[category] = findings
                network_coverage.append(coverage)
            storage_metrics, ai_usage, graph_rows = await asyncio.gather(
                optional("Storage metrics", arm_client.collect_storage_account_metrics(
                    storage_accounts, costs.period_start, costs.period_end,
                ), {}),
                optional("AI metrics", arm_client.collect_ai_deployment_usage(
                    graph_rows.get("ai_cognitive_accounts", []),
                ), {}),
                optional("SQL metrics", sql_metrics.enrich_sql_inventory(graph_rows, subscription_ids), graph_rows),
            )
            enrichment = "partial" if errors else "complete"
            message = (
                f"Reader service-principal evidence is incomplete: {', '.join(dict.fromkeys(errors))}. Missing evidence is not zero."
                if errors else "Azure evidence was read with the configured service principal. Inventory and metrics have their own collection times and retention."
            )

    report = build_full_report(
        subscription_ids, names, costs.effective_cost_by_subscription, graph_rows,
        costs.effective_cost_by_resource_id, costs.service_family_spend, advisors, untagged,
        service_spend=costs.provider_spend, network_metric_coverage=network_coverage,
        report_metadata=ReportMetadata(
            period=costs.period, periodStart=costs.period_start, periodEnd=costs.period_end,
            costBasis="Uploaded FOCUS EffectiveCost", currency=costs.currency, generatedAt=costs.generated_at,
            source="Manual FOCUS upload", staleDays=stale_days, protectedTagKeys=list(_PROTECTED_TAG_KEYS),
            enrichmentStatus=enrichment, enrichmentMessage=message, uploadStart=history.history_start,
            uploadEnd=history.history_end, assessmentMonthPartial=evidence.coverage["assessmentMonthPartial"],
            datasetId=str(dataset_id),
            billedResourceCount=len(costs.resource_costs),
        ),
        completeness=DataCompleteness(
            requestedSubscriptions=1, availableSubscriptions=1, complete=True,
            status="Uploaded FOCUS evidence; export completeness is not independently attested",
        ),
        focus_cost_data=costs, focus_history=history, advisor_score_rows=advisor_score,
        storage_accounts=storage_accounts, storage_metrics=storage_metrics,
        ai_usage_data=ai_usage, policy_compliance_rows=policy,
        include_all_resource_costs=True, cost_details_override=evidence.details, defer_tag_history=True,
    )
    report.spend_history.status_message = (
        f"Uploaded charges across {len(history.periods)} month(s). "
        "Months may be partial; upload a replacement report to change coverage."
    )
    report.daily_cost_trend.status_message = "Uploaded daily FOCUS charges; missing dates are not verified zero-cost days."
    contiguous = (date.fromisoformat(history.history_end) - date.fromisoformat(history.history_start)).days + 1 == history.complete_days
    if contiguous:
        report._anomaly_evidence = await asyncio.to_thread(detect_anomalies, history.records, history.currency)
    else:
        report.daily_cost_trend.status = "partial"
        report.daily_cost_trend.status_message = "The uploaded report has unobserved dates. Missing days are not verified zero-cost days."
        report.tag_daily_cost_trend.status = "partial"
        report.tag_daily_cost_trend.status_message = report.daily_cost_trend.status_message
    return report, evidence
