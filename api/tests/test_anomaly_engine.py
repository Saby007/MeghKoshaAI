from datetime import date, timedelta
from dataclasses import replace

from anomalies.engine import ALGORITHM_VERSION, detect_anomalies
from anomalies.models import DailyCostRecord


def _records(days: int = 61, *, spike_day: int | None = None, drop_day: int | None = None):
    start = date(2026, 6, 1)
    records = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        cost = 20 + current.weekday() * 2
        if offset == spike_day:
            cost = 100
        if offset == drop_day:
            cost = 0
        records.append(
            DailyCostRecord(
                date=str(current),
                subscription_id="sub-1",
                subscription_name="Subscription One",
                service_name="Virtual Machines",
                resource_group="rg-one",
                resource_id="/subscriptions/sub-1/resourceGroups/rg-one/providers/Microsoft.Compute/virtualMachines/vm-1",
                resource_name="vm-1",
                effective_cost=cost,
            )
        )
    return records


def test_weekly_pattern_is_not_an_anomaly():
    result = detect_anomalies(_records(), "USD")

    assert result.status == "ready"
    assert result.complete_days == 61
    assert result.anomalies == []
    assert len(result.trend) == 61
    assert result.trend[0].expected_cost is None
    assert result.trend[-1].expected_cost is not None


def test_spike_and_drop_are_deterministic():
    records = _records(spike_day=56, drop_day=57)

    first = detect_anomalies(records, "USD")
    second = detect_anomalies(records, "USD")
    first.generated_at = second.generated_at

    types = {item.anomaly_type for item in first.anomalies if item.dimension_type == "subscription"}
    assert types == {"spike", "drop"}
    assert first == second
    assert all(item.anomaly_id for item in first.anomalies)
    assert first.algorithm_version == ALGORITHM_VERSION


def test_missing_day_and_short_history_disable_detection():
    short = detect_anomalies(_records(59), "USD")
    missing = detect_anomalies(_records()[:20] + _records()[21:], "USD")

    assert short.status == "insufficient_history"
    assert short.complete_days == 59
    assert missing.status == "insufficient_history"
    assert "missing 1" in missing.status_message
    assert missing.anomalies == []


def test_zero_baseline_does_not_create_percentage_anomaly():
    records = _records()
    zero_records = [record.__class__(**{**record.__dict__, "effective_cost": 0}) for record in records]

    result = detect_anomalies(zero_records, "USD")

    assert result.status == "ready"
    assert result.anomalies == []


def test_new_resource_is_separate_from_spike():
    records = _records()
    new_date = str(date(2026, 6, 1) + timedelta(days=50))
    records.append(
        DailyCostRecord(
            date=new_date,
            subscription_id="sub-1",
            subscription_name="Subscription One",
            service_name="Storage",
            resource_group="rg-new",
            resource_id="/subscriptions/sub-1/resourceGroups/rg-new/providers/Microsoft.Storage/storageAccounts/new",
            resource_name="new",
            effective_cost=50,
        )
    )

    result = detect_anomalies(records, "USD")
    resource_items = [item for item in result.anomalies if item.dimension_type == "resource"]

    assert any(item.anomaly_type == "new_resource" and item.dimension_name == "new" for item in resource_items)


def test_late_charge_below_absolute_threshold_is_ignored():
    records = _records()
    late_day = records[-1]
    records[-1] = late_day.__class__(**{**late_day.__dict__, "effective_cost": late_day.effective_cost + 5})

    result = detect_anomalies(records, "USD")

    assert not any(item.date == late_day.date for item in result.anomalies)


def test_consecutive_same_dimension_drops_collapse_to_one_incident():
    records = _records()
    for offset in (55, 56, 57):
        record = records[offset]
        records[offset] = record.__class__(**{**record.__dict__, "effective_cost": 0})

    result = detect_anomalies(records, "USD")
    subscription_drops = [
        item
        for item in result.anomalies
        if item.dimension_type == "subscription" and item.anomaly_type == "drop"
    ]

    assert len(subscription_drops) == 1
    assert subscription_drops[0].duration_days == 3
    assert subscription_drops[0].first_detected_date == "2026-07-26"
    assert subscription_drops[0].last_detected_date == "2026-07-28"


def _ai_records(*, spike_day=None, drop_day=None):
    return [replace(
        record, service_category="AI and Machine Learning", service_name="Azure OpenAI",
        resource_type="Microsoft.CognitiveServices/accounts", resource_name="ai-account",
        resource_id="/subscriptions/sub-1/resourceGroups/rg-one/providers/Microsoft.CognitiveServices/accounts/ai-account",
    ) for record in _records(spike_day=spike_day, drop_day=drop_day)]


def test_ai_billing_has_separate_spike_and_drop_evidence():
    result = detect_anomalies(_records(spike_day=58) + _ai_records(spike_day=56, drop_day=57), "USD")
    assert result.ai_anomalies is not None
    assert {item.anomaly_type for item in result.ai_anomalies} == {"spike", "drop"}
    assert {item.dimension_type for item in result.ai_anomalies} == {"service", "resource"}
    assert all(item.dimension_name in {"Azure OpenAI", "ai-account"} for item in result.ai_anomalies)
    assert all(item.actual_cost - item.expected_cost == item.absolute_delta for item in result.ai_anomalies)
    assert all(item.baseline_samples >= 4 for item in result.ai_anomalies)
    assert result.model_dump(by_alias=True)["aiAnomalies"]


def test_ai_drop_uses_complete_calendar_when_no_ai_charge_exists_on_a_day():
    ai_records = _ai_records()
    removed_date = ai_records[57].date
    result = detect_anomalies(_records() + [record for record in ai_records if record.date != removed_date], "USD")
    assert result.status == "ready"
    assert any(item.anomaly_type == "drop" and item.date == removed_date and item.actual_cost == 0 for item in result.ai_anomalies)


def test_ai_classification_uses_resource_evidence_not_substrings_in_names():
    ai_records = [replace(record, service_category="Other", service_name="Custom billing service") for record in _ai_records(spike_day=56)]
    unrelated = [replace(record, service_name="Mail service", resource_name="ai-support-vm", resource_group="ai-workloads") for record in _records(spike_day=58)]
    result = detect_anomalies(ai_records + unrelated, "USD")
    assert result.ai_anomalies
    assert all(item.dimension_name not in {"Mail service", "ai-support-vm"} for item in result.ai_anomalies)


def test_ai_absence_is_distinct_from_insufficient_history():
    assert detect_anomalies(_records(), "USD").ai_anomalies == []
    assert detect_anomalies(_ai_records()[:30], "USD").ai_anomalies is None
    assert detect_anomalies([], "USD").ai_anomalies is None


def test_ai_unattributed_charges_do_not_invent_resource_alerts():
    records = [replace(record, resource_id="", resource_name="Unattributed") for record in _ai_records(spike_day=56)]
    result = detect_anomalies(records, "USD")
    assert result.ai_anomalies
    assert all(item.dimension_type == "service" for item in result.ai_anomalies)
