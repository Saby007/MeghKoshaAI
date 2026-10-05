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
        return runs[subscription_id] if export_name == focus_history_reader.focus_cost_reader._FOCUS_EXPORT_NAME else []

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


def _period_run(run_id: str, start: str, end: str, processed: str | None = None, export: str | None = None):
    run = {"name": run_id, "properties": {"status": "Completed", "startDate": f"{start}T00:00:00Z",
                                          "endDate": f"{end}T23:59:59Z", "processingEndTime": processed or f"{end}T23:59:59Z"}}
    return {**run, "_export": export} if export else run


def test_month_to_date_daily_run_extends_history_through_yesterday():
    runs = {SUB_1: [
        _period_run("june", "2026-06-01", "2026-06-30"),
        _period_run("july", "2026-07-01", "2026-07-31"),
        _period_run("july-to-30th", "2026-07-01", "2026-07-30", "2026-07-31T06:00:00Z", "daily"),
        _period_run("august-to-10th", "2026-08-01", "2026-08-10", "2026-08-11T06:00:00Z", "daily"),
    ]}
    available = focus_history_reader._select_available_history_runs([SUB_1], runs, 12)
    assert [(period, path) for period, _, path in available] == [
        ("2026-06", "20260601-20260630"), ("2026-07", "20260701-20260731"), ("2026-08", "20260801-20260810")]
    assert available[1][1][SUB_1]["name"] == "july"
    assert [period for period, *_ in focus_history_reader._select_history_runs([SUB_1], runs, 60)] == ["2026-06", "2026-07", "2026-08"]
    # The month to date doesn't take one of the whole-month slots.
    assert [period for period, *_ in focus_history_reader._select_available_history_runs([SUB_1], runs, 1)] == ["2026-07", "2026-08"]


def test_latest_processed_whole_month_wins_and_partial_months_only_lead():
    runs = {SUB_1: [
        _period_run("july-close", "2026-07-01", "2026-07-31", "2026-08-06T03:00:00Z"),
        _period_run("july-repull", "2026-07-01", "2026-07-31", "2026-08-04T06:00:00Z", "daily"),
        _period_run("june-partial", "2026-06-01", "2026-06-20", export="daily"),
        _period_run("august-to-3rd", "2026-08-01", "2026-08-03", export="daily"),
    ]}
    selected = focus_history_reader._select_available_history_runs([SUB_1], runs, 12)
    assert [period for period, *_ in selected] == ["2026-07", "2026-08"]
    assert selected[0][1][SUB_1]["name"] == "july-close"


def _files_for_runs(subscription_ids, selected_runs, period_path):
    return {f"{value}.csv.gz": _content(value, str(selected_runs[value]["properties"]["startDate"])[:7]) for value in subscription_ids}


def test_subscriptions_align_on_the_earliest_month_to_date_end(monkeypatch):
    runs = {
        SUB_1: [_period_run("july-1", "2026-07-01", "2026-07-31"), _period_run("aug-1", "2026-08-01", "2026-08-10", export="daily")],
        SUB_2: [_period_run("july-2", "2026-07-01", "2026-07-31"), _period_run("aug-2", "2026-08-01", "2026-08-08", export="daily")],
    }
    selected = focus_history_reader._select_available_history_runs([SUB_1, SUB_2], runs, 12)
    assert [path for *_, path in selected] == ["20260701-20260731", "20260801-20260808"]
    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_download_run_files", _files_for_runs)
    result = focus_history_reader._parse_history_files([SUB_1, SUB_2], selected)
    assert result.periods == ["2026-07", "2026-08"] and result.partial_period == "2026-08"
    assert result.history_end == "2026-08-08" and result.complete_days == 39
    assert max(record.date for record in result.records) == "2026-08-08"


def test_history_ends_where_retention_removed_older_files(monkeypatch):
    runs = {SUB_1: [_period_run("june", "2026-06-01", "2026-06-30"), _period_run("july", "2026-07-01", "2026-07-31")]}
    selected = focus_history_reader._select_available_history_runs([SUB_1], runs, 12)

    def files(subscription_ids, selected_runs, period_path):
        if period_path.startswith("202606"):
            raise focus_history_reader.focus_cost_reader.FocusCostFilesMissingError("removed by retention")
        return _files_for_runs(subscription_ids, selected_runs, period_path)

    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_download_run_files", files)
    result = focus_history_reader._parse_history_files([SUB_1], selected)
    assert result.periods == ["2026-07"] and result.history_start == "2026-07-01" and result.partial_period is None


def test_daily_export_runs_are_tagged_and_a_missing_daily_export_is_ignored(monkeypatch):
    import httpx

    async def list_runs(subscription_id, export_name):
        return [_period_run(export_name, "2026-08-01", "2026-08-03")]

    monkeypatch.setattr(focus_history_reader.arm_client, "list_cost_export_runs", list_runs)
    runs = asyncio.run(focus_history_reader._export_runs(SUB_1))
    assert [(run["name"], run.get("_export")) for run in runs] == [
        ("focus-closed-month-meghkoshaai", None), ("focus-daily-meghkoshaai", "daily")]

    async def no_daily(subscription_id, export_name):
        if export_name == "focus-daily-meghkoshaai":
            raise httpx.HTTPStatusError("missing", request=httpx.Request("GET", "https://management.azure.com"), response=httpx.Response(404))
        return [_period_run("july", "2026-07-01", "2026-07-31")]

    monkeypatch.setattr(focus_history_reader.arm_client, "list_cost_export_runs", no_daily)
    assert [run["name"] for run in asyncio.run(focus_history_reader._export_runs(SUB_1))] == ["july"]
