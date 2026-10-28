import weakref
import os
import json
import time
from datetime import date, timedelta

from anomalies.engine import detect_anomalies
from anomalies.models import DailyCostRecord
from services.history_records import ReplayableRecords
import pytest


class SyntheticHistory(ReplayableRecords):
    def __init__(self):
        self.live = 0
        self.peak = 0

    def __len__(self):
        return 6100

    def released(self):
        self.live -= 1

    def __iter__(self):
        for day in range(61):
            for resource in range(100):
                record = DailyCostRecord(
                    date=str(date(2026, 6, 1) + timedelta(days=day)), subscription_id="synthetic",
                    subscription_name="Synthetic", service_name="Azure OpenAI" if resource % 2 else "Compute",
                    resource_group="rg", resource_id=f"/subscriptions/synthetic/resources/{resource}",
                    resource_name=f"resource-{resource}", effective_cost=100 if day == 60 else 20,
                )
                self.live += 1
                self.peak = max(self.peak, self.live)
                weakref.finalize(record, self.released)
                yield record


def test_original_anomaly_algorithm_does_not_rehydrate_spooled_raw_records():
    history = SyntheticHistory()
    result = detect_anomalies(history, "USD")
    assert result.status == "ready" and result.anomalies and result.ai_anomalies
    assert history.peak <= 8, f"Detector retained {history.peak} raw records instead of bounded aggregates."
    assert history.live == 0
    subscription = next(item for item in result.anomalies if item.dimension_type == "subscription")
    assert subscription.actual_cost == 10000 and subscription.expected_cost == 2000
    assert len(subscription.contributors) == 5
    assert all(item.cost == 100 for item in subscription.contributors)


def test_million_row_anomaly_detection_keeps_raw_history_replayable():
    if os.environ.get("ASSESSMENT_RUN_SCALE_TESTS") != "1":
        pytest.skip("Enable the million-row anomaly memory gate explicitly.")
    from test_assessment_postgres import peak_process_memory

    class MillionRowHistory(ReplayableRecords):
        def __len__(self):
            return 1000000

        def __iter__(self):
            days = [str(date(2026, 6, 1) + timedelta(days=index)) for index in range(61)]
            for index in range(len(self)):
                resource = index % 15000
                day = index // 15000 % 61
                yield DailyCostRecord(
                    date=days[day], subscription_id="synthetic", subscription_name="Synthetic",
                    service_name="Azure OpenAI" if resource < 300 else "Virtual Machines",
                    resource_group="rg", resource_id=f"/subscriptions/synthetic/resources/{resource}",
                    resource_name=f"resource-{resource}", effective_cost=1000 if resource == 0 and day == 60 else 0.01,
                )

    started = time.perf_counter()
    result = detect_anomalies(MillionRowHistory(), "USD")
    peak = peak_process_memory()
    assert result.status == "ready" and result.complete_days == 61
    assert result.anomalies and result.ai_anomalies
    assert peak < 2 * 1024**3
    print(json.dumps({
        "anomalyInputRows": 1000000, "resources": 15000, "historyDays": 61,
        "seconds": time.perf_counter() - started, "peakWorkingSetBytes": peak,
        "incidentCount": len(result.anomalies), "aiIncidentCount": len(result.ai_anomalies),
    }))
