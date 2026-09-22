"""Compact, non-overlapping daily cost evidence for snapshot drill-downs."""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from brand import BRAND_NAME
from services.focus_history_reader import FocusHistoryData

from .models import CostDetailRow, CostDetailSummary, ReportSnapshot


def build_cost_details(
    history: FocusHistoryData | None,
    *,
    max_rows: int = 20000,
    max_points: int = 500000,
) -> CostDetailSummary:
    if history is None:
        return CostDetailSummary()
    start = date.fromisoformat(history.history_start)
    end = date.fromisoformat(history.history_end)
    dates = [str(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]
    allowed_dates = set(dates)
    allowed_subscriptions = set(history.subscription_ids)
    series: dict[str, tuple[dict, dict[str, Decimal]]] = {}
    point_count = 0
    for record in history.records:
        if record.date not in allowed_dates or record.subscription_id not in allowed_subscriptions:
            return CostDetailSummary(statusMessage="Daily cost detail contains out-of-scope evidence and is unavailable.")
        amount = Decimal(str(record.effective_cost))
        if not amount.is_finite():
            return CostDetailSummary(statusMessage="Daily cost detail contains invalid financial evidence and is unavailable.")
        dimensions = {
            "subscriptionId": record.subscription_id,
            "subscriptionName": record.subscription_name,
            "resourceId": record.resource_id,
            "resourceName": record.resource_name,
            "resourceType": record.resource_type,
            "resourceGroup": record.resource_group,
            "serviceName": record.service_name,
            "region": record.region or "Unassigned",
            "tags": record.tags,
            "tagAttributionSource": record.tag_attribution_source,
        }
        key = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
        if key not in series:
            series[key] = (dimensions, {})
        costs = series[key][1]
        if record.date not in costs:
            point_count += 1
        if len(series) > max_rows or point_count > max_points:
            return CostDetailSummary(statusMessage="Resource detail exceeds the snapshot detail limit. Run a report for a smaller subscription scope; headline costs remain complete.")
        costs[record.date] = costs.get(record.date, Decimal(0)) + amount
    return CostDetailSummary(
        status="complete",
        statusMessage="Complete daily FOCUS EffectiveCost, including credits and unattributed charges. Hourly amounts are daily averages, not metered hourly cost or uptime.",
        dates=dates,
        rows=[
            CostDetailRow(
                **dimensions,
                detailId=hashlib.sha256(key.encode()).hexdigest()[:24],
                dailyCosts={day: float(amount) for day, amount in sorted(costs.items())},
            )
            for key, (dimensions, costs) in sorted(series.items())
        ],
    )


def matches_cost_filter(row: CostDetailRow, filters: dict[str, str]) -> bool:
    if "requiredTagKeys" in filters:
        required = json.loads(filters["requiredTagKeys"])
        tags = {key.casefold(): value for key, value in row.tags.items()}
        missing = any(not tags.get(key.casefold(), "").strip() for key in required) if required else not any(value.strip() for value in tags.values())
        if not row.resource_id or not missing:
            return False
    fields = {
        "subscriptionId": row.subscription_id,
        "resourceId": row.resource_id,
        "resourceGroup": row.resource_group,
        "region": row.region,
        "serviceName": row.service_name,
    }
    for key, value in fields.items():
        expected = filters.get(key)
        if expected and (value != expected if key == "serviceName" else value.casefold() != expected.casefold()):
            return False
    if filters.get("tagKey") and "tagValue" in filters:
        tags = {key.casefold(): value for key, value in row.tags.items()}
        if tags.get(filters["tagKey"].casefold()) != filters["tagValue"]:
            return False
    return True


def build_cost_detail_export(
    snapshot: ReportSnapshot,
    start_date: date,
    end_date: date,
    filters: dict[str, str],
    previous_start: date | None = None,
    previous_end: date | None = None,
    selected_dates: list[date] | None = None,
):
    from io import BytesIO

    from openpyxl import Workbook

    from .exports import ReportArtifact, _sheet

    count = (end_date - start_date).days + 1
    if count < 1 or count > 366:
        raise ValueError("Select a valid cost window of at most 366 days")
    if set(filters) - {"subscriptionId", "resourceId", "resourceGroup", "region", "serviceName", "tagKey", "tagValue", "requiredTagKeys"}:
        raise ValueError("Unsupported cost filter")
    if "requiredTagKeys" in filters:
        required = json.loads(filters["requiredTagKeys"])
        if not isinstance(required, list) or len(required) > 50 or any(not isinstance(key, str) or not key.strip() or len(key) > 512 for key in required):
            raise ValueError("Required tags must be a list of at most 50 non-empty tag keys")
    if filters.get("subscriptionId") and filters["subscriptionId"].lower() not in {value.lower() for value in snapshot.subscription_ids}:
        raise ValueError("The subscription filter is outside this report")
    if (previous_start is None) != (previous_end is None):
        raise ValueError("Both comparison dates are required")
    previous_start = previous_start or start_date - timedelta(days=count)
    previous_end = previous_end or start_date - timedelta(days=1)
    if (previous_end - previous_start).days + 1 != count:
        raise ValueError("Comparison windows must have the same number of calendar days")
    details = snapshot.report.cost_details
    current_dates = {str(start_date + timedelta(days=index)) for index in range(count)}
    if selected_dates is not None:
        requested_dates = {str(day) for day in selected_dates}
        if not requested_dates or not requested_dates.issubset(current_dates):
            raise ValueError("Selected dates must be inside the requested cost window")
        current_dates = requested_dates
    previous_dates = {str(previous_start + (date.fromisoformat(day) - start_date)) for day in current_dates}
    if details.status != "complete" or not current_dates.issubset(details.dates):
        raise ValueError("Complete daily cost detail is unavailable for this window. Run a new report or choose covered dates.")
    previous_complete = previous_dates.issubset(details.dates)
    rows = [row for row in details.rows if matches_cost_filter(row, filters)]
    if sum(len((current_dates | previous_dates).intersection(row.daily_costs)) for row in rows) > 50000:
        raise ValueError("This workbook exceeds the detail export limit. Select fewer dates or a smaller subscription scope.")
    workbook = Workbook()
    workbook.remove(workbook.active)
    _sheet(workbook, "Summary", ["Field", "Value"], [
        ["Snapshot", snapshot.snapshot_id], ["Created", snapshot.created_at],
        ["Source subscriptions", ", ".join(snapshot.subscription_ids)],
        ["Current window", f"{start_date} - {end_date}"],
        ["Selected billing dates", ", ".join(sorted(current_dates))],
        ["Previous window", f"{previous_start} - {previous_end}"],
        ["Previous coverage", "Complete" if previous_complete else "Unavailable; blank comparison amounts are not zero"],
        ["Cost basis", details.cost_basis], ["Currency", snapshot.report.report_metadata.currency],
        ["Filters", json.dumps(filters, sort_keys=True)],
        ["Granularity", "Daily FOCUS charges; hourly values are daily cost / 24, not measured usage or uptime"],
        ["Tags and owner", "Exported resource tags, with current resource-group fallback only where labelled"],
        ["Budget comparison", "Native budgets use their own current billing period and ActualCost basis, not this historical EffectiveCost window"],
    ])
    daily_rows = []
    for row in rows:
        for day in sorted(current_dates):
            comparison_day = str(previous_start + (date.fromisoformat(day) - start_date))
            if day not in row.daily_costs and comparison_day not in row.daily_costs:
                continue
            amount = row.daily_costs.get(day, 0)
            before = row.daily_costs.get(comparison_day, 0) if previous_complete else None
            daily_rows.append([
                day, row.subscription_name, row.subscription_id, row.resource_name, row.resource_id,
                row.service_name, row.resource_group, row.region, json.dumps(row.tags, sort_keys=True),
                row.tag_attribution_source, amount, amount / 24, comparison_day, before,
                amount - before if before is not None else None, "Not collected",
            ])
    _sheet(workbook, "Daily resource costs", [
        "Date UTC", "Subscription", "Subscription ID", "Resource", "Resource ID", "Service", "Resource group", "Region",
        "Tags", "Tag source", "EffectiveCost", "Average hourly cost", "Comparison date", "Previous cost", "Change", "Uptime",
    ], daily_rows)
    dimensions = {
        "Resources": lambda row: row.resource_id or json.dumps([row.resource_name, row.service_name, row.resource_group]),
        "Subscriptions": lambda row: row.subscription_name,
        "Services": lambda row: row.service_name,
        "Resource types": lambda row: row.resource_type or "Unassigned",
        "Regions": lambda row: row.region,
        "Resource groups": lambda row: row.resource_group,
    }
    if filters.get("tagKey"):
        dimensions["Tags"] = lambda row: next((value for key, value in row.tags.items() if key.casefold() == filters["tagKey"].casefold()), "(Untagged)")
    for title, dimension_value in dimensions.items():
        groups: dict[tuple[str, str], list[Decimal]] = {}
        for row in rows:
            if not (current_dates | previous_dates).intersection(row.daily_costs):
                continue
            values = groups.setdefault((row.subscription_id, dimension_value(row)), [Decimal(0), Decimal(0)])
            for day, amount in row.daily_costs.items():
                if day in current_dates:
                    values[0] += Decimal(str(amount))
                if day in previous_dates:
                    values[1] += Decimal(str(amount))
        _sheet(workbook, title, ["Subscription ID", "Dimension", "Current cost", "Previous cost", "Change", "Change %"], [
            [subscription, name, float(current), float(previous) if previous_complete else None,
             float(current - previous) if previous_complete else None,
             float((current - previous) / abs(previous) * 100) if previous_complete and previous else None]
            for (subscription, name), (current, previous) in sorted(groups.items())
        ])
    output = BytesIO()
    workbook.save(output)
    return ReportArtifact(output.getvalue(), f"{BRAND_NAME}-Cost-Detail-{start_date}-{end_date}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")