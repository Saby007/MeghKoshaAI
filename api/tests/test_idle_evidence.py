from datetime import datetime, timedelta, timezone

import pytest

from findings.engine import build_report
from findings.idle_evidence import assess_idle_evidence
from services.arm_client import _metric_day_values


@pytest.mark.parametrize("category", ["stopped_vms", "unattached_disks", "empty_backend_pools", "empty_load_balancer_backend_pools"])
def test_state_only_never_establishes_confirmed_idle_even_with_cost(category):
    report = build_report(["sub-1"], 100, {category: [{"id": "/resource", "subscriptionId": "sub-1"}]}, {"/resource": 100})
    line = report.tier_a_categories[0].lines[0]
    assert line.monthly_cost == 100
    assert line.idle_evidence.classification == "candidate"
    assert line.idle_evidence.safety_review_required


def test_recent_zero_traffic_still_requires_context():
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    evidence = assess_idle_evidence("idle_nat_gateways", {"metricPeriodStart": str(end - timedelta(days=29)), "metricPeriodEnd": str(end),
        "metricDays": 30, "metricNames": ["ByteCount"], "metricTotal": 0, "evidenceType": "metrics_verified_idle"})
    assert evidence.classification == "zero_traffic_observed"
    assert evidence.safety_review_required


def test_stale_zero_traffic_is_only_a_candidate():
    evidence = assess_idle_evidence("idle_nat_gateways", {"metricPeriodStart": "2020-01-01", "metricPeriodEnd": "2020-01-31",
        "metricDays": 31, "metricNames": ["ByteCount"], "metricTotal": 0, "evidenceType": "metrics_verified_idle"})
    assert evidence.classification == "candidate"


def test_positive_traffic_and_protection_override_idle_labels():
    assert assess_idle_evidence("idle_nat_gateways", {"metricTotal": 1}).classification == "activity_observed"
    assert assess_idle_evidence("stopped_vms", {"tags": {"DoNotDelete": "true"}}).classification == "protected"


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), False])
def test_invalid_traffic_samples_cannot_sum_to_zero(value):
    data = {"value": [{"name": {"value": "ByteCount"}, "timeseries": [{"data": [{"total": value}, {"total": 0}]}]}]}
    assert _metric_day_values(data, ("ByteCount",))[0] is False