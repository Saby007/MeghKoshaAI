"""Transparent robust detector for complete daily FOCUS EffectiveCost history."""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from brand import BRAND_NAME

from .models import (
    AnomalyContributor,
    AnomalySummary,
    AnomalyTrendPoint,
    CostAnomaly,
    DailyCostRecord,
)

ALGORITHM_VERSION = "mkai-weekday-mad-v1"
REQUIRED_DAYS = 60
_MIN_BASELINE_SAMPLES = 4
_MIN_BASELINE_COST = 1.0
_MIN_ABSOLUTE_DELTA = 20.0
_MAD_MULTIPLIER = 6.0
_MIN_RELATIVE_DELTA = 0.5
_NEW_RESOURCE_MIN_COST = 10.0
_AI_CATEGORIES = frozenset({"ai and machine learning", "ai + machine learning", "ai & machine learning"})
_AI_SERVICES = frozenset({
    "azure openai", "azure ai services", "cognitive services", "azure machine learning",
    "azure ai search", "azure cognitive search", "azure ai foundry", "microsoft foundry",
})
_AI_PROVIDERS = ("microsoft.cognitiveservices", "microsoft.machinelearningservices", "microsoft.search")
DETECTION_LABEL = f"{BRAND_NAME} detection"


def _is_ai_charge(record: DailyCostRecord) -> bool:
    if record.service_category.strip().casefold() in _AI_CATEGORIES:
        return True
    if record.service_name.strip().casefold() in _AI_SERVICES:
        return True
    resource_type = record.resource_type.strip().casefold()
    resource_id = record.resource_id.casefold()
    return any(
        resource_type.startswith(f"{provider}/") or f"/providers/{provider}/" in resource_id
        for provider in _AI_PROVIDERS
    )


def _date_range(start: date, end: date) -> list[str]:
    return [str(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def _median_and_mad(values: list[float]) -> tuple[float, float]:
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return median, mad


def _severity(delta: float, expected: float) -> str:
    magnitude = abs(delta)
    relative = magnitude / expected if expected > 0 else 1.0
    if magnitude >= 100 or relative >= 2:
        return "High"
    if magnitude >= 40 or relative >= 1:
        return "Medium"
    return "Low"


def _anomaly_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _investigation_url(subscription_id: str, anomaly_date: str) -> str:
    if not subscription_id:
        return "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/costanalysis"
    scope = quote(f"/subscriptions/{subscription_id}", safe="")
    return (
        "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/CostAnalysis/"
        f"scope/{scope}/from/{anomaly_date}/to/{anomaly_date}"
    )


def _top_contributors(records: list[DailyCostRecord], anomaly_date: str) -> list[AnomalyContributor]:
    resources: dict[tuple[str, str], float] = defaultdict(float)
    for record in records:
        if record.date != anomaly_date:
            continue
        name = record.resource_name or record.service_name or "Unattributed"
        resources[(name, record.resource_id)] += record.effective_cost
    return [
        AnomalyContributor(name=name, resource_id=resource_id, cost=cost)
        for (name, resource_id), cost in sorted(
            resources.items(), key=lambda item: (-item[1], item[0][0].lower())
        )[:5]
    ]


def _trend(records: list[DailyCostRecord], ordered_days: list[str]) -> list[AnomalyTrendPoint]:
    totals: dict[str, float] = defaultdict(float)
    for record in records:
        totals[record.date] += record.effective_cost
    result: list[AnomalyTrendPoint] = []
    for index, day in enumerate(ordered_days):
        weekday = date.fromisoformat(day).weekday()
        baseline_days = [
            prior_day
            for prior_day in ordered_days[:index]
            if date.fromisoformat(prior_day).weekday() == weekday
        ][-8:]
        baseline = [totals.get(prior_day, 0.0) for prior_day in baseline_days]
        if len(baseline) < _MIN_BASELINE_SAMPLES:
            result.append(
                AnomalyTrendPoint(
                    date=day,
                    actual_cost=totals.get(day, 0.0),
                    expected_cost=None,
                    expected_lower=None,
                    expected_upper=None,
                )
            )
            continue
        expected, mad = _median_and_mad(baseline)
        robust_sigma = max(1.4826 * mad, expected * 0.05, 0.01)
        tolerance = max(_MAD_MULTIPLIER * robust_sigma, _MIN_ABSOLUTE_DELTA)
        result.append(
            AnomalyTrendPoint(
                date=day,
                actual_cost=totals.get(day, 0.0),
                expected_cost=expected,
                expected_lower=max(0.0, expected - tolerance),
                expected_upper=expected + tolerance,
            )
        )
    return result


def _series_key(record: DailyCostRecord, dimension_type: str) -> tuple[str, str, str, str]:
    if dimension_type == "subscription":
        return (
            record.subscription_id,
            record.subscription_name or record.subscription_id,
            record.subscription_id,
            record.subscription_name,
        )
    if dimension_type == "service":
        return (
            f"{record.subscription_id}:{record.service_name}",
            record.service_name or "Other",
            record.subscription_id,
            record.subscription_name,
        )
    if dimension_type == "resource_group":
        return (
            f"{record.subscription_id}:{record.resource_group}",
            record.resource_group or "Unassigned",
            record.subscription_id,
            record.subscription_name,
        )
    return (
        record.resource_id,
        record.resource_name or record.resource_id,
        record.subscription_id,
        record.subscription_name,
    )


def _collapse_incidents(anomalies: list[CostAnomaly]) -> list[CostAnomaly]:
    grouped: dict[tuple[str, str, str], list[CostAnomaly]] = defaultdict(list)
    for anomaly in anomalies:
        grouped[(anomaly.anomaly_type, anomaly.dimension_type, anomaly.dimension_id)].append(anomaly)

    incidents: list[CostAnomaly] = []
    for (anomaly_type, dimension_type, dimension_id), items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: item.date)
        clusters: list[list[CostAnomaly]] = []
        for item in ordered:
            if not clusters or (
                date.fromisoformat(item.date) - date.fromisoformat(clusters[-1][-1].date)
            ).days > 2:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        for cluster in clusters:
            representative = max(cluster, key=lambda item: abs(item.absolute_delta))
            first_date = cluster[0].date
            last_date = cluster[-1].date
            incidents.append(
                representative.model_copy(
                    update={
                        "anomaly_id": _anomaly_id(
                            ALGORITHM_VERSION,
                            dimension_type,
                            dimension_id,
                            first_date,
                            last_date,
                            anomaly_type,
                        ),
                        "first_detected_date": first_date,
                        "last_detected_date": last_date,
                        "duration_days": (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days + 1,
                    }
                )
            )
    return incidents


def _detect_series(
    records: list[DailyCostRecord],
    dimension_type: str,
    ordered_days: list[str],
) -> list[CostAnomaly]:
    series: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    record_groups: dict[tuple[str, str, str, str], list[DailyCostRecord]] = defaultdict(list)
    for record in records:
        key = _series_key(record, dimension_type)
        series[key][record.date] += record.effective_cost
        record_groups[key].append(record)

    anomalies: list[CostAnomaly] = []
    for (dimension_id, dimension_name, subscription_id, subscription_name), values in sorted(series.items()):
        seen_nonzero = False
        for index, day in enumerate(ordered_days):
            actual = values.get(day, 0.0)
            weekday = date.fromisoformat(day).weekday()
            baseline_days = [
                prior_day
                for prior_day in ordered_days[:index]
                if date.fromisoformat(prior_day).weekday() == weekday
            ][-8:]
            baseline = [values.get(prior_day, 0.0) for prior_day in baseline_days]

            anomaly_type = ""
            expected = lower = upper = delta = 0.0
            percentage_delta: float | None = None
            if dimension_type == "resource" and actual >= _NEW_RESOURCE_MIN_COST and not seen_nonzero:
                prior_total = sum(values.get(prior_day, 0.0) for prior_day in ordered_days[:index])
                if index >= 14 and prior_total == 0:
                    anomaly_type = "new_resource"
                    delta = actual
                    upper = actual
            elif len(baseline) >= _MIN_BASELINE_SAMPLES:
                expected, mad = _median_and_mad(baseline)
                if expected >= _MIN_BASELINE_COST:
                    robust_sigma = max(1.4826 * mad, expected * 0.05, 0.01)
                    tolerance = max(_MAD_MULTIPLIER * robust_sigma, _MIN_ABSOLUTE_DELTA)
                    lower = max(0.0, expected - tolerance)
                    upper = expected + tolerance
                    delta = actual - expected
                    percentage_delta = delta / expected
                    if actual > upper and percentage_delta >= _MIN_RELATIVE_DELTA:
                        anomaly_type = "spike"
                    elif actual < lower and percentage_delta <= -_MIN_RELATIVE_DELTA:
                        anomaly_type = "drop"

            if actual > 0:
                seen_nonzero = True
            if not anomaly_type:
                continue
            anomalies.append(
                CostAnomaly(
                    anomaly_id=_anomaly_id(ALGORITHM_VERSION, dimension_type, dimension_id, day, anomaly_type),
                    date=day,
                    first_detected_date=day,
                    last_detected_date=day,
                    duration_days=1,
                    anomaly_type=anomaly_type,
                    dimension_type=dimension_type,
                    dimension_name=dimension_name,
                    dimension_id=dimension_id,
                    subscription_id=subscription_id,
                    subscription_name=subscription_name,
                    actual_cost=actual,
                    expected_cost=expected,
                    expected_lower=lower,
                    expected_upper=upper,
                    absolute_delta=delta,
                    percentage_delta=percentage_delta,
                    severity=_severity(delta, expected),
                    baseline_samples=len(baseline),
                    contributors=_top_contributors(record_groups[(dimension_id, dimension_name, subscription_id, subscription_name)], day),
                    investigation_url=_investigation_url(subscription_id, day),
                )
            )
    return anomalies


def detect_anomalies(records: list[DailyCostRecord], currency: str) -> AnomalySummary:
    generated_at = datetime.now(timezone.utc).isoformat()
    if not records:
        return AnomalySummary(
            algorithm_version=ALGORITHM_VERSION,
            label=DETECTION_LABEL,
            status="insufficient_history",
            status_message="No complete daily FOCUS history is available.",
            history_start="",
            history_end="",
            complete_days=0,
            required_days=REQUIRED_DAYS,
            currency=currency,
            generated_at=generated_at,
            trend=[],
            anomalies=[],
        )

    observed_days = {record.date for record in records}
    start = date.fromisoformat(min(observed_days))
    end = date.fromisoformat(max(observed_days))
    expected_days = _date_range(start, end)
    missing_days = sorted(set(expected_days) - observed_days)
    if len(expected_days) < REQUIRED_DAYS or missing_days:
        detail = (
            f"History contains {len(expected_days)} calendar days."
            if not missing_days
            else f"History is missing {len(missing_days)} calendar day(s)."
        )
        return AnomalySummary(
            algorithm_version=ALGORITHM_VERSION,
            label=DETECTION_LABEL,
            status="insufficient_history",
            status_message=detail,
            history_start=str(start),
            history_end=str(end),
            complete_days=len(expected_days) - len(missing_days),
            required_days=REQUIRED_DAYS,
            currency=currency,
            generated_at=generated_at,
            trend=[],
            anomalies=[],
        )

    anomalies: list[CostAnomaly] = []
    for dimension_type in ("subscription", "service", "resource_group", "resource"):
        anomalies.extend(_detect_series(records, dimension_type, expected_days))
    anomalies = _collapse_incidents(anomalies)
    anomalies.sort(
        key=lambda item: (item.last_detected_date, abs(item.absolute_delta), item.dimension_name),
        reverse=True,
    )
    ai_records = [record for record in records if _is_ai_charge(record)]
    ai_anomalies = _collapse_incidents(
        _detect_series(ai_records, "service", expected_days)
        + _detect_series([record for record in ai_records if record.resource_id], "resource", expected_days)
    )
    ai_anomalies.sort(
        key=lambda item: (item.last_detected_date, abs(item.absolute_delta), item.dimension_name),
        reverse=True,
    )
    return AnomalySummary(
        algorithm_version=ALGORITHM_VERSION,
        label=DETECTION_LABEL,
        status="ready",
        status_message="Deterministic detection over complete daily FOCUS EffectiveCost history.",
        history_start=str(start),
        history_end=str(end),
        complete_days=len(expected_days),
        required_days=REQUIRED_DAYS,
        currency=currency,
        generated_at=generated_at,
        trend=_trend(records, expected_days),
        anomalies=anomalies,
        ai_anomalies=ai_anomalies,
    )
