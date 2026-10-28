"""Bounded SQL cost projections for the original report components."""

import calendar
import json
import re
from datetime import date, timedelta
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field, JsonValue

from services.assessment_limits import AssessmentLimitError
from services.assessment_settings import settings
from reports.models import CostDetailSummary


class CostWindow(BaseModel):
    start_date: date = Field(alias="startDate")
    end_date: date = Field(alias="endDate")
    month_to_date: bool = Field(default=False, alias="monthToDate")


class BudgetQuery(BaseModel):
    subscription_id: str = Field(alias="subscriptionId", max_length=64)
    name: str = Field(max_length=512)
    filter: JsonValue = None


CostDimension = Literal["resource", "subscription", "service", "resourceType", "region", "resourceGroup", "tag"]


class CostSelection(BaseModel):
    dimension: CostDimension
    source_id: str = Field(alias="sourceId", max_length=8192)
    tag_key: str = Field(default="", alias="tagKey", max_length=512)


class CostQuery(BaseModel):
    kind: Literal["groups", "metrics", "daily", "facets", "sources", "budget", "budgetRelations"]
    window: CostWindow | None = None
    previous: CostWindow | None = None
    filters: dict[str, str] = Field(default_factory=dict, max_length=8)
    constraints: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    dimension: CostDimension = "resource"
    group_constraints: list[CostSelection] = Field(default_factory=list, alias="groupConstraints", max_length=8)
    sort: Literal["cost", "change"] = "cost"
    nonzero: bool = False
    current_only: bool = Field(default=False, alias="currentOnly")
    observed_only: bool = Field(default=False, alias="observedOnly")
    spikes_only: bool = Field(default=False, alias="spikesOnly")
    untagged_only: bool = Field(default=False, alias="untaggedOnly")
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=50, ge=1, le=200)
    tag_key: str = Field(default="", alias="tagKey", max_length=512)
    source_id: str = Field(default="", alias="sourceId", max_length=8192)
    budget: BudgetQuery | None = None
    tag_pairs: bool = Field(default=False, alias="tagPairs")
    budgets: list[BudgetQuery] = Field(default_factory=list, max_length=100)
    require_evidence: bool = Field(default=False, alias="requireEvidence")


def _dates(window: CostWindow) -> list[date]:
    days = (window.end_date - window.start_date).days + 1
    if days < 1:
        raise ValueError("Cost window dates must be ordered.")
    if days > 366:
        raise ValueError("Cost windows must contain at most 366 days.")
    return [window.start_date + timedelta(days=index) for index in range(days)]


def date_windows(query: CostQuery) -> tuple[list[date], list[date]]:
    if query.window is None:
        raise ValueError("A cost window is required.")
    current = _dates(query.window)
    if query.previous is not None:
        return current, _dates(query.previous)
    first, last = current[0], current[-1]
    whole_month = first.day == 1 and last == date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    mtd = query.window.month_to_date and first.day == 1 and (first.year, first.month) == (last.year, last.month)
    if whole_month or mtd:
        month_end = first - timedelta(days=1)
        end_day = month_end.day if whole_month else min(last.day, month_end.day)
        previous = CostWindow(startDate=date(month_end.year, month_end.month, 1),
                              endDate=date(month_end.year, month_end.month, end_day))
    else:
        previous = CostWindow(startDate=first - timedelta(days=len(current)), endDate=first - timedelta(days=1))
    return current, _dates(previous)


def fold_tag(key: str) -> str:
    return re.sub(r"[\s_-]+", " ", key.lower()).strip()


def normalized_tags(tags: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in tags.items():
        result.setdefault(fold_tag(key), value)
    return result


def row_predicate(filters: list[dict[str, str]], arguments: list) -> str:
    predicate = ["r.tenant_id=$1", "r.snapshot_id=$2"]
    columns = {
        "subscriptionId": ("r.subscription_id", True),
        "resourceId": ("r.resource_id", True),
        "serviceName": ("r.service_name", False),
        "resourceGroup": ("lower(r.resource_group)", True),
        "region": ("lower(r.region)", True),
    }

    def parameter(value) -> str:
        arguments.append(value)
        return f"${len(arguments)}"

    for scope in filters:
        if set(scope) - set(columns) - {"tagKey", "tagValue", "requiredTagKeys"}:
            raise ValueError("Unsupported cost filter.")
        if any(len(value) > 8192 for value in scope.values()):
            raise ValueError("Cost filter values are too long.")
        for key, (column, insensitive) in columns.items():
            if scope.get(key):
                value = scope[key].lower() if insensitive else scope[key]
                predicate.append(f"{column}={parameter(value)}")
        if scope.get("tagKey") and "tagValue" in scope:
            key = parameter(fold_tag(scope["tagKey"]))
            value = parameter(scope["tagValue"])
            predicate.append(f"r.normalized_tags->>{key}={value}")
        if "requiredTagKeys" in scope:
            try:
                keys = json.loads(scope["requiredTagKeys"])
            except json.JSONDecodeError as error:
                raise ValueError("requiredTagKeys must be a JSON string array.") from error
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                raise ValueError("requiredTagKeys must be a JSON string array.")
            if len(keys) > 100:
                raise ValueError("requiredTagKeys contains too many keys.")
            predicate.append("r.resource_id<>''")
            if keys:
                parameter_keys = parameter([fold_tag(key) for key in keys])
                predicate.append(
                    f"EXISTS(SELECT 1 FROM unnest({parameter_keys}::text[]) k "
                    "WHERE btrim(coalesce(r.normalized_tags->>k,''))='')"
                )
            else:
                predicate.append(
                    "NOT EXISTS(SELECT 1 FROM jsonb_each_text(r.metadata->'tags') t "
                    "WHERE btrim(t.value)<>'')"
                )
    return " AND ".join(predicate)


async def persist_costs(connection: asyncpg.Connection, tenant: str, snapshot_id: str, details: CostDetailSummary) -> None:
    if details.status != "complete":
        return
    rows = []
    for ordinal, row in enumerate(details.rows):
        if not row.detail_id:
            raise ValueError("Complete cost evidence must have a stable detail ID.")
        metadata = row.model_dump(by_alias=True, mode="json", exclude={"daily_costs"})
        resource_key = (row.resource_id or json.dumps(
            [row.resource_name, row.service_name, row.resource_group], separators=(",", ":"), ensure_ascii=False
        )).lower()
        rows.append((
            tenant, snapshot_id, row.detail_id, ordinal, row.subscription_id.lower(),
            row.resource_id.lower(), resource_key, row.resource_group, row.service_name,
            row.region, row.resource_type, json.dumps(normalized_tags(row.tags)), json.dumps(metadata),
        ))
        if len(rows) == 100:
            await connection.executemany(
                "INSERT INTO assessments.cost_rows VALUES "
                "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb)", rows,
            )
            rows.clear()
    if rows:
        await connection.executemany(
            "INSERT INTO assessments.cost_rows VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb)", rows,
        )
    # Uncommitted bulk rows need fresh stats before foreign-key lookups are planned.
    await connection.execute("SELECT assessments.refresh_serving_statistics(false)")
    dates = {day: date.fromisoformat(day) for day in details.dates}
    # Bound foreign-key trigger work per COPY without weakening atomic publication.
    points = []
    for row in details.rows:
        for day, amount in row.daily_costs.items():
            if day not in dates:
                raise ValueError("A daily charge is outside the complete evidence date axis.")
            points.append((tenant, snapshot_id, row.detail_id, dates[day], amount))
            if len(points) == 5000:
                await connection.copy_records_to_table(
                    "cost_daily", schema_name="assessments", records=points,
                    columns=("tenant_id", "snapshot_id", "detail_id", "charge_date", "amount"), timeout=300,
                )
                points.clear()
    if points:
        await connection.copy_records_to_table(
            "cost_daily", schema_name="assessments", records=points,
            columns=("tenant_id", "snapshot_id", "detail_id", "charge_date", "amount"), timeout=300,
        )


def _bounded_page(base: dict, rows: list[dict], offset: int) -> dict:
    items = []
    size = len(json.dumps(base).encode()) + 2048
    for row in rows:
        encoded = len(json.dumps(row, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
        if size + encoded > settings().response_bytes:
            if not items:
                raise AssessmentLimitError("A cost projection row exceeds the response budget.")
            break
        size += encoded
        items.append(row)
    return {**base, "items": items, "nextOffset": offset + len(items) if offset + len(items) < base["total"] else None}


def _base(query: CostQuery, tenant: str, snapshot_id: str) -> tuple[str, list]:
    arguments = [tenant, snapshot_id]
    predicate = row_predicate([*query.constraints, query.filters], arguments)
    if query.untagged_only:
        predicate += " AND NOT EXISTS(SELECT 1 FROM jsonb_each_text(r.metadata->'tags') t WHERE btrim(t.value)<>'')"
    selections = list(query.group_constraints)
    if query.source_id:
        selections.append(CostSelection(dimension=query.dimension, sourceId=query.source_id,
                                        tagKey=query.filters.get("tagKey") or query.tag_key))
    for selection in selections:
        try:
            identity = json.loads(selection.source_id)
        except json.JSONDecodeError as error:
            raise ValueError("Invalid selected cost group.") from error
        if (not isinstance(identity, list) or len(identity) != 2 or not isinstance(identity[0], str)
                or identity[1] is not None and not isinstance(identity[1], str)):
            raise ValueError("Invalid selected cost group.")
        dimension = _dimension(query, arguments, selection)
        arguments.extend([identity[0].lower(), identity[1]])
        predicate += (
            f" AND r.subscription_id=${len(arguments)-1} "
            f"AND {dimension} IS NOT DISTINCT FROM ${len(arguments)}::text"
        )
    return f"SELECT r.* FROM assessments.cost_rows r WHERE {predicate}", arguments


def _dimension(query: CostQuery, arguments: list, selection: CostSelection | None = None) -> str:
    columns = {
        "resource": "r.resource_key", "subscription": "r.subscription_id",
        "service": "r.service_name", "resourceType": "r.resource_type",
        "region": "r.region", "resourceGroup": "lower(r.resource_group)",
    }
    dimension = selection.dimension if selection is not None else query.dimension
    if dimension == "tag":
        arguments.append(fold_tag(selection.tag_key if selection is not None else query.filters.get("tagKey") or query.tag_key))
        return f"r.normalized_tags->>${len(arguments)}"
    return columns[dimension]


async def _owners(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery,
                  groups: list, dates: list[date]) -> dict[int, list[str]]:
    if not groups:
        return {}
    selected, arguments = _base(query, tenant, snapshot_id)
    dimension = _dimension(query, arguments)
    arguments.append(dates)
    dates_parameter = len(arguments)
    arguments.append(json.dumps([
        {"group_index": index, "subscription_id": row["subscription_id"], "group_value": row["group_value"]}
        for index, row in enumerate(groups)
    ]))
    rows = await connection.fetch(
        f"WITH selected AS ({selected}) SELECT w.group_index,t.value,min(r.ordinal) AS first_ordinal "
        "FROM selected r JOIN "
        f"jsonb_to_recordset(${len(arguments)}::jsonb) AS w(group_index integer,subscription_id text,group_value text) "
        f"ON r.subscription_id=w.subscription_id AND {dimension} IS NOT DISTINCT FROM w.group_value "
        "CROSS JOIN LATERAL jsonb_each_text(r.metadata->'tags') t "
        "WHERE lower(t.key)=ANY(ARRAY['owner','applicationowner','technicalowner','businessowner']) "
        "AND EXISTS(SELECT 1 FROM assessments.cost_daily d WHERE d.tenant_id=r.tenant_id "
        "AND d.snapshot_id=r.snapshot_id AND d.detail_id=r.detail_id "
        f"AND d.charge_date=ANY(${dates_parameter}::date[])) "
        "GROUP BY w.group_index,t.value ORDER BY first_ordinal,t.value", *arguments,
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["group_index"], []).append(row["value"])
    return result


async def cost_groups(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery,
                      available_dates: list[str]) -> dict:
    current, previous = date_windows(query)
    available = set(available_dates)
    covered = all(str(day) in available for day in current)
    previous_covered = all(str(day) in available for day in previous)
    selected, arguments = _base(query, tenant, snapshot_id)
    dimension = _dimension(query, arguments)
    arguments.extend([current, previous])
    now_parameter, before_parameter = len(arguments) - 1, len(arguments)
    cte = (
        f"WITH selected AS ({selected}), amounts AS ("
        "SELECT d.detail_id,"
        f"sum(d.amount) FILTER(WHERE d.charge_date=ANY(${now_parameter}::date[])) AS current,"
        f"sum(d.amount) FILTER(WHERE d.charge_date=ANY(${before_parameter}::date[])) AS previous,"
        f"bool_or(d.charge_date=ANY(${now_parameter}::date[])) AS has_current "
        "FROM assessments.cost_daily d JOIN selected r USING(tenant_id,snapshot_id,detail_id) "
        f"WHERE d.tenant_id=$1 AND d.snapshot_id=$2 AND (d.charge_date=ANY(${now_parameter}::date[]) "
        f"OR d.charge_date=ANY(${before_parameter}::date[])) GROUP BY d.detail_id), grouped AS ("
        f"SELECT r.subscription_id,{dimension} AS group_value,min(r.ordinal) AS first_ordinal,"
        "sum(coalesce(a.current,0)) AS current,sum(coalesce(a.previous,0)) AS previous,count(*) AS sources,"
        "bool_or(a.has_current) AS has_current,count(DISTINCT r.resource_key) AS resource_count "
        "FROM selected r JOIN amounts a USING(detail_id) "
        f"GROUP BY r.subscription_id,{dimension}), groups AS (SELECT * FROM grouped WHERE true"
        + (" AND (current<>0 OR previous<>0)" if query.nonzero else "")
        + (" AND has_current" if query.current_only else "")
        + (" AND previous>0 AND (current-previous)/nullif(abs(previous),0)>0.3" if query.spikes_only and covered and previous_covered else
           " AND false" if query.spikes_only else "") + ") "
    )
    totals = await connection.fetchrow(
        cte + "SELECT count(*) AS total,coalesce(sum(current),0) AS current,"
        "coalesce(sum(previous),0) AS previous,greatest(0,coalesce(max(current),0)) AS largest,"
        "count(DISTINCT subscription_id) AS subscriptions,"
        "count(*) FILTER(WHERE previous>0 AND (current-previous)/nullif(abs(previous),0)>0.3) AS spikes FROM groups", *arguments,
    )
    metadata = {
        "total": totals["total"], "currentTotal": totals["current"] if covered else 0,
        "previousTotal": totals["previous"] if previous_covered else 0,
        "largest": totals["largest"] if covered else 0,
        "spikeCount": totals["spikes"] if covered and previous_covered else 0,
        "subscriptionCount": totals["subscriptions"],
        "observedCurrentTotal": totals["current"],
    }
    if query.kind == "metrics":
        return metadata
    order = (
        ("abs(g.current-g.previous) DESC," if covered and previous_covered else "")
        + ("abs(g.current) DESC" if covered else "g.first_ordinal")
    ) if query.sort == "change" else (
        "abs(g.current) DESC" if covered else "abs(g.previous) DESC" if previous_covered else "g.first_ordinal"
    )
    arguments.extend([query.offset, min(query.limit, settings().page_size)])
    rows = await connection.fetch(
        cte + "SELECT g.*,r.metadata FROM groups g JOIN selected r ON r.ordinal=g.first_ordinal "
        f"ORDER BY {order},g.subscription_id,g.group_value NULLS LAST "
        f"OFFSET ${len(arguments)-1} LIMIT ${len(arguments)}", *arguments,
    )
    owners = await _owners(connection, tenant, snapshot_id, query, rows, sorted(set(current + previous)))
    items = []
    for index, row in enumerate(rows):
        metadata_row = json.loads(row["metadata"])
        current_value = row["current"] if covered else None
        previous_value = row["previous"] if previous_covered else None
        delta = current_value - previous_value if current_value is not None and previous_value is not None else None
        value = row["group_value"]
        name = (metadata_row["resourceName"] or "Unattributed charge") if query.dimension == "resource" else (
            metadata_row["subscriptionName"] if query.dimension == "subscription" else
            metadata_row["resourceGroup"] if query.dimension == "resourceGroup" else value if value is not None else "Untagged"
        )
        items.append({
            "id": json.dumps([row["subscription_id"], value], separators=(",", ":"), ensure_ascii=False),
            "name": name, "subscriptionId": metadata_row["subscriptionId"], "subscriptionName": metadata_row["subscriptionName"],
            "current": current_value, "previous": previous_value, "delta": delta,
            "percentage": delta / abs(previous_value) * 100 if delta is not None and previous_value else None,
            "sources": [metadata_row], "sourceCount": row["sources"], "owners": owners.get(index, []),
            "hasCurrent": row["has_current"],
            "resourceCount": row["resource_count"],
        })
    return _bounded_page(metadata, items, query.offset)


async def daily_costs(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery,
                      available_dates: list[str]) -> dict:
    current, previous = date_windows(query)
    selected, arguments = _base(query, tenant, snapshot_id)
    arguments.append(sorted(set(current + previous)))
    rows = await connection.fetch(
        f"WITH selected AS ({selected}) SELECT r.subscription_id,min(r.ordinal) AS ordinal,"
        "(array_agg(r.metadata->>'subscriptionName' ORDER BY r.ordinal))[1] AS subscription_name,"
        "d.charge_date,sum(d.amount) AS amount FROM selected r JOIN assessments.cost_daily d "
        "USING(tenant_id,snapshot_id,detail_id) WHERE d.tenant_id=$1 AND d.snapshot_id=$2 "
        f"AND d.charge_date=ANY(${len(arguments)}::date[]) "
        "GROUP BY r.subscription_id,d.charge_date ORDER BY ordinal,d.charge_date", *arguments,
    )
    subscriptions = await connection.fetch(
        f"WITH selected AS ({selected}) SELECT subscription_id,"
        "(array_agg(metadata->>'subscriptionName' ORDER BY ordinal))[1] AS name "
        "FROM selected GROUP BY subscription_id ORDER BY min(ordinal)", *arguments[:-1],
    )
    totals = {(row["subscription_id"], str(row["charge_date"])): row["amount"] for row in rows}
    available = set(available_dates)
    result = []
    calendar_comparison = query.previous is None and ((
        query.window.month_to_date and current[0].day == 1
        and (current[0].year, current[0].month) == (current[-1].year, current[-1].month)
    ) or (
        current[0].day == 1 and current[-1].day == calendar.monthrange(current[0].year, current[0].month)[1]
    ))
    for subscription in subscriptions:
        days = []
        for index, day in enumerate(current):
            before = previous[index] if index < len(previous) else None
            if query.previous is not None and index >= len(previous):
                before = None
            elif not calendar_comparison and before is None:
                before = previous[0] + timedelta(days=index)
            days.append({
                "date": str(day), "previousDate": str(before) if before else "",
                "current": totals.get((subscription["subscription_id"], str(day)), None if query.observed_only else 0) if str(day) in available else None,
                "previous": totals.get((subscription["subscription_id"], str(before)), None if query.observed_only else 0) if before and str(before) in available else None,
            })
        result.append({"subscriptionId": subscription["subscription_id"], "subscriptionName": subscription["name"], "days": days})
    payload = {"series": result}
    if len(json.dumps(payload).encode()) > settings().response_bytes:
        raise AssessmentLimitError("Daily cost series exceeds the response budget; narrow the selected scope.")
    return payload


async def cost_facets(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery) -> dict:
    selected, arguments = _base(query, tenant, snapshot_id)
    arguments.append(fold_tag(query.tag_key))
    tag_parameter = len(arguments)
    cte = (
        f"WITH selected AS ({selected}), facets AS ("
        "SELECT 'subscriptionId' AS field,subscription_id AS value,"
        "(array_agg(metadata->>'subscriptionName' ORDER BY ordinal DESC))[1] AS label "
        "FROM selected GROUP BY subscription_id UNION "
        "SELECT 'serviceName',service_name,service_name FROM selected UNION "
        "SELECT 'resourceGroup',resource_group,resource_group FROM selected UNION "
        "SELECT 'region',region,region FROM selected UNION "
        "SELECT 'tagKey',t.key,t.key FROM selected r CROSS JOIN LATERAL jsonb_each_text(r.metadata->'tags') t UNION "
        f"SELECT 'tagValue',normalized_tags->>${tag_parameter},normalized_tags->>${tag_parameter} "
        f"FROM selected WHERE normalized_tags ? ${tag_parameter}"
        + (" UNION SELECT 'tagPair',json_build_array(t.key,t.value)::text,t.key || ' / ' || t.value "
           "FROM selected r CROSS JOIN LATERAL jsonb_each_text(r.metadata->'tags') t" if query.tag_pairs else "") + ") "
    )
    total = await connection.fetchval(cte + "SELECT count(*) FROM facets", *arguments)
    arguments.extend([query.offset, min(query.limit, settings().page_size)])
    rows = await connection.fetch(
        cte + f"SELECT * FROM facets ORDER BY field,value OFFSET ${len(arguments)-1} LIMIT ${len(arguments)}",
        *arguments,
    )
    items = [dict(row) for row in rows]
    for item in items:
        if item["field"] == "tagPair":
            item["value"] = json.dumps(json.loads(item["value"]), separators=(",", ":"), ensure_ascii=False)
    return _bounded_page({"total": total}, items, query.offset)


async def cost_sources(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery) -> dict:
    if not query.source_id:
        raise ValueError("A selected cost group is required.")
    current, previous = date_windows(query)
    selected, arguments = _base(query, tenant, snapshot_id)
    arguments.append(sorted(set(current + previous)))
    cte = (
        f"WITH selected AS ({selected}), sources AS (SELECT r.* FROM selected r WHERE EXISTS("
        "SELECT 1 FROM assessments.cost_daily d WHERE d.tenant_id=r.tenant_id "
        "AND d.snapshot_id=r.snapshot_id AND d.detail_id=r.detail_id "
        f"AND d.charge_date=ANY(${len(arguments)}::date[]))) "
    )
    total = await connection.fetchval(cte + "SELECT count(*) FROM sources", *arguments)
    arguments.extend([query.offset, min(query.limit, settings().page_size)])
    rows = await connection.fetch(
        cte + f"SELECT metadata FROM sources ORDER BY ordinal OFFSET ${len(arguments)-1} LIMIT ${len(arguments)}",
        *arguments,
    )
    return _bounded_page({"total": total}, [json.loads(row["metadata"]) for row in rows], query.offset)


def budget_predicate(expression: JsonValue, arguments: list, depth: int = 0) -> str:
    if not isinstance(expression, dict) or depth > 16:
        return "NULL::boolean"
    if not expression:
        return "true"
    if len(expression) != 1:
        return "NULL::boolean"
    kind, condition = next(iter(expression.items()))
    if kind in {"and", "or"}:
        if not isinstance(condition, list) or not condition:
            return "NULL::boolean"
        operator = " AND " if kind == "and" else " OR "
        return "(" + operator.join(budget_predicate(item, arguments, depth + 1) for item in condition) + ")"
    if kind not in {"dimensions", "tags"} or not isinstance(condition, dict):
        return "NULL::boolean"
    name, operator, values = (condition.get(key) for key in ("name", "operator", "values"))
    if (not isinstance(name, str) or operator != "In" or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)):
        return "NULL::boolean"
    if kind == "tags":
        arguments.extend([fold_tag(name), values])
        return f"coalesce(r.normalized_tags->>${len(arguments)-1}=ANY(${len(arguments)}::text[]),false)"
    columns = {
        "resourceid": "r.resource_id", "resourcegroupname": "lower(r.resource_group)",
        "resourcegroup": "lower(r.resource_group)", "servicename": "lower(r.service_name)",
        "resourcelocation": "lower(r.region)", "subscriptionid": "r.subscription_id",
    }
    if name.lower() not in columns:
        return "NULL::boolean"
    arguments.append([value.lower() for value in values])
    return f"{columns[name.lower()]}=ANY(${len(arguments)}::text[])"


async def budget_daily(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery) -> dict:
    if query.budget is None:
        raise ValueError("Budget metadata is required.")
    current, _ = date_windows(query)
    selected, arguments = _base(query, tenant, snapshot_id)
    arguments.append(query.budget.subscription_id.lower())
    subscription_parameter = len(arguments)
    match = budget_predicate(query.budget.filter, arguments)
    cte = (
        f"WITH selected AS ({selected}), judged AS (SELECT r.*,{match} AS matches FROM selected r "
        f"WHERE r.subscription_id=${subscription_parameter}), verified AS ("
        "SELECT NOT coalesce(bool_or(matches IS NULL),false) AND "
        + ("true" if "filter" in query.budget.model_fields_set else "false") +
        " AS known FROM judged), selected_budget AS ("
        "SELECT j.* FROM judged j CROSS JOIN verified v WHERE NOT v.known OR j.matches) "
    )
    verified = await connection.fetchval(cte + "SELECT known FROM verified", *arguments)
    arguments.append(current)
    rows = await connection.fetch(
        cte + "SELECT d.charge_date,sum(d.amount) AS amount FROM selected_budget r "
        "JOIN assessments.cost_daily d USING(tenant_id,snapshot_id,detail_id) "
        f"WHERE d.tenant_id=$1 AND d.snapshot_id=$2 AND d.charge_date=ANY(${len(arguments)}::date[]) "
        "GROUP BY d.charge_date", *arguments,
    )
    totals = {row["charge_date"]: row["amount"] for row in rows}
    return {"dates": [str(day) for day in current], "values": [totals.get(day) for day in current], "verified": verified}


async def budget_relations(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery) -> dict:
    selected, base_arguments = _base(query, tenant, snapshot_id)
    result = []
    for budget in query.budgets:
        if query.filters.get("subscriptionId") and query.filters["subscriptionId"].lower() != budget.subscription_id.lower():
            continue
        arguments = [*base_arguments, budget.subscription_id.lower()]
        subscription_parameter = len(arguments)
        match = budget_predicate(budget.filter, arguments)
        row = await connection.fetchrow(
            f"WITH selected AS ({selected}) SELECT count(*) AS total,"
            f"coalesce(bool_or({match}),false) AS matches,"
            f"coalesce(bool_or(({match}) IS NULL),false) AS unknown FROM selected r "
            f"WHERE r.subscription_id=${subscription_parameter}", *arguments,
        )
        if query.require_evidence and not row["total"]:
            continue
        relation = ("Subscription-wide budget" if budget.filter == {} else
                    "Matching budget filter" if row["matches"] else
                    "Filter applicability unverified" if not row["total"] or row["unknown"] else None)
        if relation is not None:
            result.append({"subscriptionId": budget.subscription_id, "name": budget.name, "relation": relation})
    return {"relations": result}


async def execute(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: CostQuery,
                  available_dates: list[str]) -> dict:
    if query.kind in {"groups", "metrics"}:
        return await cost_groups(connection, tenant, snapshot_id, query, available_dates)
    if query.kind == "daily":
        return await daily_costs(connection, tenant, snapshot_id, query, available_dates)
    if query.kind == "facets":
        return await cost_facets(connection, tenant, snapshot_id, query)
    if query.kind == "sources":
        return await cost_sources(connection, tenant, snapshot_id, query)
    if query.kind == "budget":
        return await budget_daily(connection, tenant, snapshot_id, query)
    if query.kind == "budgetRelations":
        return await budget_relations(connection, tenant, snapshot_id, query)
    raise ValueError("Unsupported cost query kind.")
