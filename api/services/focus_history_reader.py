"""Load multi-period daily FOCUS history: closed months plus, when available, the month to date."""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from anomalies.models import DailyCostRecord
from services import arm_client, focus_cost_reader
from services.focus_export_control import normalize_subscription_id

logger = logging.getLogger(__name__)

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
    # The month Azure hasn't closed yet (data runs through history_end); month totals must skip it.
    partial_period: str | None = None


def _calendar_days(start: date, end: date) -> list[str]:
    return [str(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _run_bounds(run: dict) -> tuple[str, date, date] | None:
    """Period, start and end of a completed run that starts on the first day of one calendar month."""
    properties = run.get("properties") or {}
    if properties.get("status") != "Completed":
        return None
    try:
        start = date.fromisoformat(str(properties["startDate"])[:10])
        end = date.fromisoformat(str(properties["endDate"])[:10])
    except (KeyError, ValueError):
        return None
    if start.day != 1 or (start.year, start.month) != (end.year, end.month):
        return None
    return start.strftime("%Y-%m"), start, end


def _contiguous_periods(
    subscription_ids: list[str],
    runs_by_subscription: dict[str, list[dict]],
) -> list[tuple[str, dict[str, dict], date, date]]:
    """Newest-first periods every subscription covers without gaps.

    Older periods need a whole-month run; only the newest may be month to date. When the closed-month
    and daily exports both have a run for a month, the most recently processed one wins.
    """
    candidates: dict[str, dict[str, list[tuple[dict, date, date]]]] = {value: {} for value in subscription_ids}
    for subscription_id in subscription_ids:
        for run in runs_by_subscription.get(subscription_id, []):
            bounds = _run_bounds(run)
            if bounds:
                candidates[subscription_id].setdefault(bounds[0], []).append((run, bounds[1], bounds[2]))

    def processed(item: tuple[dict, date, date]) -> str:
        return str((item[0].get("properties") or {}).get("processingEndTime") or "")

    selected: list[tuple[str, dict[str, dict], date, date]] = []
    previous_start: date | None = None
    for period in sorted({period for by_period in candidates.values() for period in by_period}, reverse=True):
        picks: dict[str, tuple[dict, date, date]] = {}
        for subscription_id in subscription_ids:
            options = candidates[subscription_id].get(period, [])
            whole = [item for item in options if item[2] == _month_end(item[1])]
            pool = whole or ([] if selected else options)
            if pool:
                picks[subscription_id] = max(pool, key=processed)
        if len(picks) != len(subscription_ids):
            if selected:
                break
            continue
        start = next(iter(picks.values()))[1]
        end = min(item[2] for item in picks.values())
        if previous_start is not None and end + timedelta(days=1) != previous_start:
            break
        selected.append((period, {key: item[0] for key, item in picks.items()}, start, end))
        previous_start = start
    return selected


def _period_path(start: date, end: date) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def _select_history_runs(
    subscription_ids: list[str],
    runs_by_subscription: dict[str, list[dict]],
    required_days: int,
) -> list[tuple[str, dict[str, dict], str]]:
    periods = _contiguous_periods(subscription_ids, runs_by_subscription)
    if not periods:
        raise focus_cost_reader.FocusCostDataError(
            "No completed FOCUS history period contains every selected subscription"
        )
    selected: list[tuple[str, dict[str, dict], str]] = []
    covered_days = 0
    for period, runs, start, end in periods:
        selected.append((period, runs, _period_path(start, end)))
        covered_days += (end - start).days + 1
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
    periods = _contiguous_periods(subscription_ids, runs_by_subscription)
    if not periods:
        raise focus_cost_reader.FocusCostDataError(
            "No completed FOCUS history period contains every selected subscription"
        )
    selected: list[tuple[str, dict[str, dict], str]] = []
    whole_months = 0
    for period, runs, start, end in periods:
        # The month to date rides along without taking one of the whole-month slots.
        if whole_months >= max_periods:
            break
        selected.append((period, runs, _period_path(start, end)))
        whole_months += end == _month_end(start)
    return list(reversed(selected))


def _parse_history_files(
    subscription_ids: list[str],
    selected_periods: list[tuple[str, dict[str, dict], str]],
    resource_group_tags: dict[tuple[str, str], dict[str, str]] | None = None,
) -> FocusHistoryData:
    resource_group_tags = resource_group_tags or {}
    currencies: set[str] = set()
    subscription_names: dict[str, str] = {}
    observed_days: dict[str, set[str]] = {value: set() for value in subscription_ids}
    parsed: list[tuple[str, list[DailyCostRecord], date, date]] = []
    seen_periods: set[str] = set()

    # Newest first, so history can end cleanly where storage retention removed older files.
    for period, selected_runs, period_path in reversed(selected_periods):
        if period in seen_periods:
            raise focus_cost_reader.FocusCostDataError("Duplicate FOCUS history period")
        seen_periods.add(period)
        month_start = date.fromisoformat(f"{period}-01")
        month_end = _month_end(month_start)
        ends: list[date] = []
        for subscription_id in subscription_ids:
            properties = selected_runs[subscription_id].get("properties") or {}
            try:
                end = date.fromisoformat(str(properties.get("endDate", ""))[:10])
            except ValueError:
                end = None
            # Only the newest period may still be open (month to date); older ones must be whole months.
            if (str(properties.get("startDate", ""))[:10] != str(month_start) or end is None
                    or not month_start <= end <= month_end or (parsed and end != month_end)):
                raise focus_cost_reader.FocusCostDataError("FOCUS history requires complete calendar-month runs")
            ends.append(end)
        period_end = min(ends)
        try:
            files = focus_cost_reader._download_run_files(subscription_ids, selected_runs, period_path)
        except focus_cost_reader.FocusCostFilesMissingError:
            if parsed:
                break
            continue
        period_records: list[DailyCostRecord] = []
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
                if parsed_date > period_end:
                    # Another subscription's month-to-date run ends earlier; keep every subscription aligned.
                    continue
                currency = row.get("BillingCurrency") or ""
                if currency:
                    currencies.add(currency)
                subscription_names[subscription_id] = row.get("SubAccountName") or subscription_id
                observed_days[subscription_id].add(charge_date)
                resource_tags = focus_cost_reader._tags(row.get("Tags"))
                group_tags = resource_group_tags.get((subscription_id, str(row.get("x_ResourceGroupName") or "").strip().lower()), {})
                inherited = {key: value for key, value in group_tags.items() if not resource_tags.get(key)}
                tags = {**resource_tags, **inherited}
                period_records.append(
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
        parsed.append((period, period_records, month_start, period_end))

    parsed.reverse()
    records = [record for _, period_records, _, _ in parsed for record in period_records]

    if len(currencies) != 1:
        raise focus_cost_reader.FocusCostDataError(
            f"Expected one FOCUS history currency, found: {sorted(currencies)}"
        )
    if not records:
        raise focus_cost_reader.FocusCostDataError("FOCUS history contains no rows")

    start = parsed[0][2]
    end = parsed[-1][3]
    expected_days = set(_calendar_days(start, end))
    for subscription_id in subscription_ids:
        missing = sorted(expected_days - observed_days[subscription_id])
        if missing:
            raise focus_cost_reader.FocusCostDataError(
                f"FOCUS history is missing {len(missing)} day(s) for {subscription_id}"
            )

    newest_period, _, newest_start, newest_end = parsed[-1]
    return FocusHistoryData(
        currency=next(iter(currencies)),
        history_start=str(start),
        history_end=str(end),
        complete_days=len(expected_days),
        periods=[period for period, *_ in parsed],
        subscription_ids=subscription_ids,
        subscription_names=subscription_names,
        records=records,
        partial_period=newest_period if newest_end != _month_end(newest_start) else None,
    )


async def _export_runs(subscription_id: str) -> list[dict]:
    monthly = await arm_client.list_cost_export_runs(subscription_id, focus_cost_reader._FOCUS_EXPORT_NAME)
    try:
        daily = await arm_client.list_cost_export_runs(subscription_id, focus_cost_reader._FOCUS_DAILY_EXPORT_NAME)
    except (httpx.HTTPError, ValueError) as error:
        # The daily export is optional; without it history simply ends at the last closed month.
        if not (isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 404):
            logger.warning("Daily FOCUS export history unavailable for %s", subscription_id, exc_info=True)
        daily = []
    return [*monthly, *({**run, "_export": "daily"} for run in daily)]


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

    run_lists = await asyncio.gather(*(_export_runs(value) for value in normalized))
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

    run_lists = await asyncio.gather(*(_export_runs(value) for value in normalized))
    selected = _select_available_history_runs(
        normalized,
        dict(zip(normalized, run_lists)),
        max_periods=max_periods,
    )
    group_tags = await focus_cost_reader._load_resource_group_tags(normalized)
    result = await asyncio.to_thread(_parse_history_files, normalized, selected, group_tags)
    _cache[cache_key] = (now, result)
    return result
