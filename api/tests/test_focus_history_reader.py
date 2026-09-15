import asyncio
import csv
import gzip
import io
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import focus_history_reader
from services.focus_cost_reader import FocusCostDataError

SUB_1 = "616dc9b8-b4aa-415f-8dcb-71bc462916c5"
SUB_2 = "6efdb685-f5ce-4be4-9841-83d09c4ac05a"


def _run(period: str, run_id: str):
    start = date.fromisoformat(f"{period}-01")
    if period == "2026-06":
        end = date(2026, 6, 30)
    else:
        end = date(2026, 7, 31)
    return {
        "name": run_id,
        "properties": {
            "status": "Completed",
            "startDate": f"{start}T00:00:00Z",
            "endDate": f"{end}T23:59:59Z",
            "processingEndTime": f"{end}T23:59:59Z",
        },
    }


def test_selects_two_contiguous_common_periods_for_60_days():
    runs = {
        SUB_1: [_run("2026-06", "june-1"), _run("2026-07", "july-1")],
        SUB_2: [_run("2026-06", "june-2"), _run("2026-07", "july-2")],
    }

    selected = focus_history_reader._select_history_runs([SUB_1, SUB_2], runs, 60)
    available = focus_history_reader._select_available_history_runs([SUB_1, SUB_2], runs, 12)

    assert [item[0] for item in selected] == ["2026-06", "2026-07"]
    assert [item[0] for item in available] == ["2026-06", "2026-07"]


def test_rejects_noncontiguous_or_partial_history():
    runs = {
        SUB_1: [_run("2026-06", "june-1"), _run("2026-07", "july-1")],
        SUB_2: [_run("2026-07", "july-2")],
    }

    with pytest.raises(FocusCostDataError, match="31 days"):
        focus_history_reader._select_history_runs([SUB_1, SUB_2], runs, 60)


def _content(subscription_id: str, period: str, missing_day: int | None = None) -> bytes:
    from services.focus_cost_reader import _REQUIRED_COLUMNS

    start = date.fromisoformat(f"{period}-01")
    days = 30 if period == "2026-06" else 31
    columns = sorted(_REQUIRED_COLUMNS | {"ChargePeriodStart"})
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for offset in range(days):
        if missing_day == offset + 1:
            continue
        current = start + timedelta(days=offset)
        row = {column: "" for column in columns}
        row.update(
            {
                "BilledCost": "1",
                "BillingCurrency": "USD",
                "BillingPeriodStart": f"{start}T00:00:00Z",
                "BillingPeriodEnd": f"{start + timedelta(days=days)}T00:00:00Z",
                "ChargeCategory": "Usage",
                "ChargeClass": "",
                "ChargePeriodStart": f"{current}T00:00:00Z",
                "ContractedCost": "1",
                "ContractedUnitPrice": "1",
                "EffectiveCost": "1",
                "ListCost": "1",
                "ListUnitPrice": "1",
                "PricingCategory": "Standard",
                "PricingCurrency": "USD",
                "PricingQuantity": "1",
                "PricingUnit": "1 Hour",
                "ResourceId": f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
                "ResourceName": "vm",
                "ResourceType": "Microsoft.Compute/virtualMachines",
                "ServiceCategory": "Compute",
                "ServiceName": "Virtual Machines",
                "SkuId": "sku",
                "SkuPriceId": "price",
                "SubAccountId": f"/subscriptions/{subscription_id}",
                "SubAccountName": subscription_id,
                "x_EffectiveUnitPrice": "1",
                "x_ResourceGroupName": "rg",
                "x_SkuMeterCategory": "Virtual Machines",
                "x_SkuMeterId": "meter",
                "x_SkuMeterSubcategory": "Dv5",
                "x_SkuServiceFamily": "Compute",
            }
        )
        writer.writerow(row)
    return gzip.compress(stream.getvalue().encode())


def test_loads_complete_daily_history_and_rejects_missing_subscription_day(monkeypatch):
    runs = {
        SUB_1: [_run("2026-06", "june-1"), _run("2026-07", "july-1")],
        SUB_2: [_run("2026-06", "june-2"), _run("2026-07", "july-2")],
    }
    files = {}
    for subscription_id, period_runs in runs.items():
        for run in period_runs:
            period = str(run["properties"]["startDate"])[:7]
            period_path = "20260601-20260630" if period == "2026-06" else "20260701-20260731"
            name = f"focus/{subscription_id}/focus-closed-month-meghkoshaai/{period_path}/{run['name']}/part.csv.gz"
            files[name] = _content(subscription_id, period)

    async def list_runs(subscription_id, export_name):
        return runs[subscription_id]

    async def group_tags(subscription_ids):
        return {}

    class Container:
        def list_blobs(self, name_starts_with):
            return [
                SimpleNamespace(name=name, size=len(content), last_modified=datetime.now(timezone.utc))
                for name, content in files.items()
                if name.startswith(name_starts_with)
            ]

        def download_blob(self, name):
            return SimpleNamespace(readall=lambda: files[name])

    monkeypatch.setattr(focus_history_reader.arm_client, "list_cost_export_runs", list_runs)
    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_load_resource_group_tags", group_tags)
    monkeypatch.setattr(
        focus_history_reader.focus_cost_reader.focus_export_download,
        "get_container_client",
        lambda: Container(),
    )
    focus_history_reader._cache.clear()

    result = asyncio.run(focus_history_reader.load_complete_focus_history([SUB_1, SUB_2]))

    assert result.complete_days == 61
    assert result.periods == ["2026-06", "2026-07"]
    assert len(result.records) == 122
    assert result.history_start == "2026-06-01"
    assert result.history_end == "2026-07-31"
    assert result.records[0].service_category == "Compute"
    assert result.records[0].resource_type == "Microsoft.Compute/virtualMachines"
    assert result.records[0].region == "Unassigned"


@pytest.mark.parametrize("missing_day", [1, 15, 31])
def test_history_requires_month_edges_not_just_observed_date_range(monkeypatch, missing_day):
    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_download_run_files", lambda *args: {"part.csv.gz": _content(SUB_1, "2026-07", missing_day)})
    with pytest.raises(FocusCostDataError, match="missing"):
        focus_history_reader._parse_history_files([SUB_1], [("2026-07", {SUB_1: _run("2026-07", "run")}, "20260701-20260731")])


def test_history_group_tag_fallback_keeps_resource_tags_and_provenance(monkeypatch):
    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_download_run_files", lambda *args: {"part.csv.gz": _content(SUB_1, "2026-07")})
    original_reader = focus_history_reader.focus_cost_reader._csv_reader

    def tagged_reader(content, blob_name):
        for row in original_reader(content, blob_name):
            row["Tags"] = '{"Team":"Resource Owner"}'
            yield row

    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_csv_reader", tagged_reader)
    result = focus_history_reader._parse_history_files([SUB_1], [("2026-07", {SUB_1: _run("2026-07", "run")}, "20260701-20260731")],
        {(SUB_1, "rg"): {"team": "Group Owner", "project": "Current Group Project"}})
    assert result.records[0].tags == {"team": "Resource Owner", "project": "Current Group Project"}
    assert result.records[0].tag_attribution_source == "exported_resource_tags_with_current_group_fallback"
