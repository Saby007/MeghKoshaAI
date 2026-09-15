"""Load complete multi-period daily FOCUS history for anomaly detection."""

from __future__ import annotations

import asyncio
import calendar
import time
from dataclasses import dataclass
from datetime import date, timedelta

from anomalies.models import DailyCostRecord
from services import arm_client, focus_cost_reader
from services.focus_export_control import normalize_subscription_id

_cache: dict[tuple[str, ...], tuple[float, "FocusHistoryData"]] = {}
_CACHE_TTL_SECONDS = 3600.0


@dataclass(frozen=True)
class FocusHistoryData:
    currency: str
    history_start: str
    history_end: str
    complete_days: int
    periods: list[str]
    subscription_ids: list[str]
    subscription_names: dict[str, str]
    records: list[DailyCostRecord]


def _calendar_days(start: date, end: date) -> list[str]:
    return [str(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def _select_history_runs(
    subscription_ids: list[str],
    runs_by_subscription: dict[str, list[dict]],
    required_days: int,
) -> list[tuple[str, dict[str, dict], str]]:
    completed: dict[str, dict[str, list[dict]]] = {}
    for subscription_id in subscription_ids:
        periods: dict[str, list[dict]] = {}
        for run in runs_by_subscription.get(subscription_id, []):
            run_period = focus_cost_reader._run_period(run)
            if run_period:
                periods.setdefault(run_period[0], []).append(run)
        completed[subscription_id] = periods

    common_periods = set.intersection(*(set(completed[value]) for value in subscription_ids)) if subscription_ids else set()
    if not common_periods:
        raise focus_cost_reader.FocusCostDataError(
            "No completed FOCUS history period contains every selected subscription"
        )

    selected: list[tuple[str, dict[str, dict], str]] = []
    covered_days = 0
    previous_start: date | None = None
    for period in sorted(common_periods, reverse=True):
        selected_runs: dict[str, dict] = {}
        period_path = ""
        period_start: date | None = None
        period_end: date | None = None
        for subscription_id in subscription_ids:
            run = max(
                completed[subscription_id][period],
                key=lambda item: str((item.get("properties") or {}).get("processingEndTime") or ""),
            )
            run_period = focus_cost_reader._run_period(run)
            assert run_period is not None
            selected_runs[subscription_id] = run
            period_path = f"{run_period[1]}-{run_period[2]}"
            period_start = date.fromisoformat(str((run.get("properties") or {})["startDate"])[:10])
            period_end = date.fromisoformat(str((run.get("properties") or {})["endDate"])[:10])
        assert period_start is not None and period_end is not None
        if previous_start is not None and period_end + timedelta(days=1) != previous_start:
            break
        selected.append((period, selected_runs, period_path))
        covered_days += (period_end - period_start).days + 1
        previous_start = period_start
        if covered_days >= required_days:
            return list(reversed(selected))

    raise focus_cost_reader.FocusCostDataError(
        f"Complete contiguous FOCUS history has {covered_days} days; {required_days} are required"
    )


def _select_available_history_runs(
    subscription_ids: list[str],
    runs_by_subscription: dict[str, list[dict]],
    max_periods: int = 12,
) -> list[tuple[str, dict[str, dict], str]]:
    completed: dict[str, dict[str, list[dict]]] = {}
    for subscription_id in subscription_ids:
        periods: dict[str, list[dict]] = {}
        for run in runs_by_subscription.get(subscription_id, []):
            run_period = focus_cost_reader._run_period(run)
            if run_period:
                periods.setdefault(run_period[0], []).append(run)
        completed[subscription_id] = periods

    common_periods = set.intersection(*(set(completed[value]) for value in subscription_ids)) if subscription_ids else set()
    if not common_periods:
        raise focus_cost_reader.FocusCostDataError(
            "No completed FOCUS history period contains every selected subscription"
        )

    selected: list[tuple[str, dict[str, dict], str]] = []
    previous_start: date | None = None
    for period in sorted(common_periods, reverse=True):
        selected_runs: dict[str, dict] = {}
        period_path = ""
        period_start: date | None = None
        period_end: date | None = None
        for subscription_id in subscription_ids:
            run = max(
                completed[subscription_id][period],
                key=lambda item: str((item.get("properties") or {}).get("processingEndTime") or ""),
            )
            run_period = focus_cost_reader._run_period(run)
            assert run_period is not None
            selected_runs[subscription_id] = run
            period_path = f"{run_period[1]}-{run_period[2]}"
            period_start = date.fromisoformat(str((run.get("properties") or {})["startDate"])[:10])
            period_end = date.fromisoformat(str((run.get("properties") or {})["endDate"])[:10])
        assert period_start is not None and period_end is not None
        if previous_start is not None and period_end + timedelta(days=1) != previous_start:
            break
        selected.append((period, selected_runs, period_path))
        previous_start = period_start
        if len(selected) >= max_periods:
            break
    return list(reversed(selected))


def _parse_history_files(
    subscription_ids: list[str],
    selected_periods: list[tuple[str, dict[str, dict], str]],
    resource_group_tags: dict[tuple[str, str], dict[str, str]] | None = None,
) -> FocusHistoryData:
    resource_group_tags = resource_group_tags or {}
    records: list[DailyCostRecord] = []
    currencies: set[str] = set()
    subscription_names: dict[str, str] = {}
    observed_days: dict[str, set[str]] = {value: set() for value in subscription_ids}
    period_names: list[str] = []
    selected_days: set[str] = set()

    for period, selected_runs, period_path in selected_periods:
        if period in period_names:
            raise focus_cost_reader.FocusCostDataError("Duplicate FOCUS history period")
        month_start = date.fromisoformat(f"{period}-01")
        month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])
        for subscription_id in subscription_ids:
            properties = selected_runs[subscription_id].get("properties") or {}
            if str(properties.get("startDate", ""))[:10] != str(month_start) or str(properties.get("endDate", ""))[:10] != str(month_end):
                raise focus_cost_reader.FocusCostDataError("FOCUS history requires complete calendar-month runs")
        selected_days.update(_calendar_days(month_start, month_end))
        period_names.append(period)
        files = focus_cost_reader._download_run_files(subscription_ids, selected_runs, period_path)
        for blob_name, content in sorted(files.items()):
            for row in focus_cost_reader._csv_reader(content, blob_name):
                subscription_id = focus_cost_reader._subscription_id(row["SubAccountId"])
                if subscription_id not in observed_days:
                    raise focus_cost_reader.FocusCostDataError("FOCUS history contains an out-of-scope subscription")
                charge_date = str(row.get("ChargePeriodStart") or "")[:10]
                try:
                    parsed_date = date.fromisoformat(charge_date)
                except ValueError as error:
                    raise focus_cost_reader.FocusCostDataError(
                        f"FOCUS history row has invalid ChargePeriodStart {charge_date!r}"
                    ) from error
                if parsed_date.strftime("%Y-%m") != period:
                    raise focus_cost_reader.FocusCostDataError(
                        f"FOCUS history row date {charge_date} does not match run period {period}"
                    )
                currency = row.get("BillingCurrency") or ""
                if currency:
                    currencies.add(currency)
                subscription_names[subscription_id] = row.get("SubAccountName") or subscription_id
                observed_days[subscription_id].add(charge_date)
                resource_tags = focus_cost_reader._tags(row.get("Tags"))
                group_tags = resource_group_tags.get((subscription_id, str(row.get("x_ResourceGroupName") or "").strip().lower()), {})
                inherited = {key: value for key, value in group_tags.items() if not resource_tags.get(key)}
                tags = {**resource_tags, **inherited}
                records.append(
                    DailyCostRecord(
                        date=charge_date,
                        subscription_id=subscription_id,
                        subscription_name=subscription_names[subscription_id],
                        service_name=row.get("ServiceName") or "Other",
                        resource_group=row.get("x_ResourceGroupName") or "Unassigned",
                        resource_id=(row.get("ResourceId") or "").lower(),
                        resource_name=row.get("ResourceName") or "Unattributed",
                        effective_cost=float(focus_cost_reader._decimal(row.get("EffectiveCost"), "EffectiveCost")),
                        service_category=row.get("ServiceCategory") or "Other",
                        resource_type=row.get("x_ResourceType") or row.get("ResourceType") or "",
                        region=focus_cost_reader._focus_region(row),
                        charge_category=row.get("ChargeCategory") or "",
                        list_cost=float(focus_cost_reader._decimal(row.get("ListCost"), "ListCost")),
                        contracted_cost=float(focus_cost_reader._decimal(row.get("ContractedCost"), "ContractedCost")),
                        commitment_discount_type=row.get("CommitmentDiscountType") or "",
                        commitment_discount_status=row.get("CommitmentDiscountStatus") or "",
                        pricing_category=row.get("PricingCategory") or "",
                        tags=tags,
                        tag_attribution_source="exported_resource_tags_with_current_group_fallback" if inherited else "exported_resource_tags",
                    )
                )

    if len(currencies) != 1:
        raise focus_cost_reader.FocusCostDataError(
            f"Expected one FOCUS history currency, found: {sorted(currencies)}"
        )
    if not records:
        raise focus_cost_reader.FocusCostDataError("FOCUS history contains no rows")

    start = date.fromisoformat(min(selected_days))
    end = date.fromisoformat(max(selected_days))
    expected_days = set(_calendar_days(start, end))
    for subscription_id in subscription_ids:
        missing = sorted(expected_days - observed_days[subscription_id])
        if missing:
            raise focus_cost_reader.FocusCostDataError(
                f"FOCUS history is missing {len(missing)} day(s) for {subscription_id}"
            )

    return FocusHistoryData(
        currency=next(iter(currencies)),
        history_start=str(start),
        history_end=str(end),
        complete_days=len(expected_days),
        periods=period_names,
        subscription_ids=subscription_ids,
        subscription_names=subscription_names,
        records=records,
    )


async def load_complete_focus_history(
    subscription_ids: list[str],
    *,
    required_days: int = 60,
) -> FocusHistoryData:
    normalized = [normalize_subscription_id(value) for value in subscription_ids]
    cache_key = tuple(sorted(normalized)) + (str(required_days),)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    run_lists = await asyncio.gather(
        *(arm_client.list_cost_export_runs(value, focus_cost_reader._FOCUS_EXPORT_NAME) for value in normalized)
    )
    selected = _select_history_runs(normalized, dict(zip(normalized, run_lists)), required_days)
    group_tags = await focus_cost_reader._load_resource_group_tags(normalized)
    result = await asyncio.to_thread(_parse_history_files, normalized, selected, group_tags)
    if result.complete_days < required_days:
        raise focus_cost_reader.FocusCostDataError(
            f"FOCUS history has {result.complete_days} complete days; {required_days} are required"
        )
    _cache[cache_key] = (now, result)
    return result


async def load_available_focus_history(
    subscription_ids: list[str],
    *,
    max_periods: int = 12,
) -> FocusHistoryData:
    normalized = [normalize_subscription_id(value) for value in subscription_ids]
    cache_key = tuple(sorted(normalized)) + ("dashboard", str(max_periods))
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    run_lists = await asyncio.gather(
        *(arm_client.list_cost_export_runs(value, focus_cost_reader._FOCUS_EXPORT_NAME) for value in normalized)
    )
    selected = _select_available_history_runs(
        normalized,
        dict(zip(normalized, run_lists)),
        max_periods=max_periods,
    )
    group_tags = await focus_cost_reader._load_resource_group_tags(normalized)
    result = await asyncio.to_thread(_parse_history_files, normalized, selected, group_tags)
    _cache[cache_key] = (now, result)
    return result
