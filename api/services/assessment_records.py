"""Scoped record projections and full-result statistics for existing report views."""

import json
import re
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field

from services.assessment_limits import AssessmentLimitError
from services.assessment_settings import settings


class RecordSum(BaseModel):
    field: str = Field(max_length=128)
    equals: dict[str, str] = Field(default_factory=dict, max_length=8)
    operation: Literal["sum", "max"] = "sum"


class ArrayMatch(BaseModel):
    path: str = Field(max_length=128)
    field: str = Field(max_length=128)
    value: str = Field(max_length=2048)


class RecordQuery(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    include_paths: list[str] | None = Field(default=None, alias="includePaths", max_length=100)
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=50, ge=1, le=5000)
    search: str = Field(default="", max_length=100)
    search_fields: list[str] = Field(default_factory=lambda: [
        "resourceName", "resourceId", "subscriptionName", "detail", "@displayName", "evidenceType",
    ], alias="searchFields", max_length=20)
    search_paths: list[str] = Field(default_factory=list, alias="searchPaths", max_length=100)
    search_aliases: dict[str, list[str]] = Field(default_factory=dict, alias="searchAliases", max_length=8)
    equals: dict[str, str] = Field(default_factory=dict, max_length=20)
    one_of: dict[str, list[str]] = Field(default_factory=dict, alias="oneOf", max_length=20)
    has_arrays: list[str] = Field(default_factory=list, alias="hasArrays", max_length=10)
    array_matches: list[ArrayMatch] = Field(default_factory=list, alias="arrayMatches", max_length=10)
    sums: dict[str, RecordSum] = Field(default_factory=dict, max_length=10)
    facets: dict[str, str] = Field(default_factory=dict, max_length=10)
    facets_only: bool = Field(default=False, alias="facetsOnly")
    facet_order: Literal["label", "source"] = Field(default="label", alias="facetOrder")
    by_path: bool = Field(default=False, alias="byPath")
    metadata_only: bool = Field(default=False, alias="metadataOnly")


class HierarchyQuery(BaseModel):
    subscription_id: str | None = Field(default=None, alias="subscriptionId", max_length=64)
    resource_group: str | None = Field(default=None, alias="resourceGroup", max_length=512)


def _parameter(arguments: list, value) -> str:
    arguments.append(value)
    return f"${len(arguments)}"


def _field(field: str, arguments: list, *, as_json: bool = False) -> str:
    if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", field):
        raise ValueError("Invalid report field.")
    context = field.startswith("@")
    parts = field.lstrip("@").split(".")
    return f"r.{'context' if context else 'payload'} {'#>' if as_json else '#>>'} {_parameter(arguments, parts)}::text[]"


def _equals(conditions: dict[str, str], arguments: list) -> list[str]:
    return [f"{_field(field, arguments)}={_parameter(arguments, value)}" for field, value in conditions.items()]


def _predicate(query: RecordQuery, arguments: list) -> str:
    clauses = _equals(query.equals, arguments)
    if query.include_paths is not None:
        clauses.append(f"r.path=ANY({_parameter(arguments, query.include_paths)}::text[])")
    for field, values in query.one_of.items():
        if len(values) > 200:
            raise ValueError("Too many report filter values.")
        clauses.append(f"{_field(field, arguments)}=ANY({_parameter(arguments, values)}::text[])")
    for field in query.has_arrays:
        value = _field(field, arguments, as_json=True)
        clauses.append(f"CASE WHEN jsonb_typeof({value})='array' THEN jsonb_array_length({value})>0 ELSE false END")
    for match in query.array_matches:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", match.field):
            raise ValueError("Invalid report array field.")
        value = _field(match.path, arguments, as_json=True)
        field = _parameter(arguments, match.field)
        expected = _parameter(arguments, match.value)
        clauses.append(
            "EXISTS(SELECT 1 FROM jsonb_array_elements("
            f"CASE WHEN jsonb_typeof({value})='array' THEN {value} ELSE '[]'::jsonb END"
            f") a WHERE a->>{field}={expected})"
        )
    if query.search:
        pattern = "%" + query.search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        parameter = _parameter(arguments, pattern)
        searches = [f"lower(coalesce({_field(field, arguments)},'')) LIKE {parameter}" for field in query.search_fields]
        if query.search_paths:
            searches.append(f"r.path=ANY({_parameter(arguments, query.search_paths)}::text[])")
        for field, values in query.search_aliases.items():
            if len(values) > 200:
                raise ValueError("Too many report search aliases.")
            searches.append(f"{_field(field, arguments)}=ANY({_parameter(arguments, values)}::text[])")
        if not searches:
            raise ValueError("Report search requires at least one field.")
        clauses.append("(" + " OR ".join(searches) + ")")
    return " AND ".join(clauses) if clauses else "true"


async def project(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: RecordQuery,
                  manifest: list[dict]) -> dict:
    available = {item["path"] for item in manifest}
    if not set(query.paths) <= available:
        raise ValueError("Requested report collection is not available in this snapshot.")
    if query.include_paths is not None and not set(query.include_paths) <= set(query.paths):
        raise ValueError("Report path filters must belong to the requested collections.")
    if not set(query.search_paths) <= set(query.paths):
        raise ValueError("Report search paths must belong to the requested collections.")
    arguments = [tenant, snapshot_id, query.paths]
    source = (
        "WITH source AS (SELECT r.*,coalesce(s.collection_context->r.path,'{}'::jsonb) AS context "
        "FROM assessments.records r JOIN assessments.snapshots s USING(tenant_id,snapshot_id) "
        "WHERE r.tenant_id=$1 AND r.snapshot_id=$2 AND r.path=ANY($3::text[]) AND s.published), "
    )
    where = _predicate(query, arguments)
    base = source + f"filtered AS (SELECT r.* FROM source r WHERE {where}) "
    aggregate_arguments = list(arguments)
    aggregates = []
    for index, specification in enumerate(query.sums.values()):
        raw = _field(specification.field, aggregate_arguments, as_json=True)
        text = _field(specification.field, aggregate_arguments)
        conditions = _equals(specification.equals, aggregate_arguments)
        condition = " AND ".join(conditions) if conditions else "true"
        aggregates.append(
            f"coalesce({specification.operation}(CASE WHEN {condition} AND jsonb_typeof({raw})='number' "
            f"THEN ({text})::double precision ELSE 0 END),0) AS sum_{index}"
        )
    stats = await connection.fetchrow(
        base + "SELECT count(*) AS total,(SELECT count(*) FROM source) AS unfiltered"
        + ("," + ",".join(aggregates) if aggregates else "") + " FROM filtered r", *aggregate_arguments,
    )
    by_path = {}
    if query.by_path:
        rows = await connection.fetch(
            base + "SELECT r.path,count(*) AS total" + ("," + ",".join(aggregates) if aggregates else "")
            + " FROM filtered r GROUP BY r.path", *aggregate_arguments,
        )
        by_path = {row["path"]: {
            "total": row["total"],
            "sums": {name: row[f"sum_{index}"] for index, name in enumerate(query.sums)},
        } for row in rows}
    if query.facets_only:
        if len(query.facets) != 1:
            raise ValueError("A paged report facet requires exactly one field.")
        facet_arguments = list(arguments)
        field, label_field = next(iter(query.facets.items()))
        value = _field(field, facet_arguments)
        label = _field(label_field, facet_arguments)
        faceted = base + (
            f",faceted AS (SELECT {value} AS value,{label} AS label,min(ordinal) AS first_ordinal "
            f"FROM filtered r GROUP BY {value},{label}) "
        )
        total = await connection.fetchval(faceted + "SELECT count(*) FROM faceted", *facet_arguments)
        order = "first_ordinal,value" if query.facet_order == "source" else "label,value"
        facet_arguments.extend([query.offset, min(query.limit, 5000), settings().response_bytes - 2048])
        rows = await connection.fetch(
            faceted + f",candidates AS (SELECT * FROM faceted ORDER BY {order} "
            f"OFFSET ${len(facet_arguments)-2} LIMIT ${len(facet_arguments)-1}), sized AS ("
            f"SELECT *,sum(octet_length(coalesce(to_json(value)::text,'null'))+octet_length(coalesce(to_json(label)::text,'null'))+128) OVER(ORDER BY {order}) AS bytes "
            f"FROM candidates) SELECT value,label FROM sized WHERE bytes<=${len(facet_arguments)} ORDER BY {order}",
            *facet_arguments,
        )
        if total > query.offset and not rows:
            raise AssessmentLimitError("A report facet exceeds the response budget.")
        last = query.offset + len(rows)
        return {
            "total": total, "unfilteredTotal": total, "sums": {}, "byPath": {}, "facets": {},
            "items": [{"path": "", "value": dict(row), "context": {}} for row in rows],
            "nextOffset": last if last < total else None,
        }
    facets = {}
    for field, label_field in query.facets.items():
        facet_arguments = [tenant, snapshot_id, query.paths]
        value = _field(field, facet_arguments)
        label = _field(label_field, facet_arguments)
        rows = await connection.fetch(
            source + "unused AS (SELECT 1) "
            f"SELECT DISTINCT {value} AS value,{label} AS label FROM source r "
            "ORDER BY label,value", *facet_arguments,
        )
        facets[field] = [[row["value"], row["label"]] for row in rows]
    result = {
        "total": stats["total"], "unfilteredTotal": stats["unfiltered"],
        "sums": {name: stats[f"sum_{index}"] for index, name in enumerate(query.sums)},
        "facets": facets, "byPath": by_path, "items": [], "nextOffset": None,
    }
    if query.metadata_only:
        if len(json.dumps(result).encode()) > settings().response_bytes:
            raise AssessmentLimitError("Report filter metadata exceeds the response budget.")
        return result


    arguments.extend([query.offset, min(query.limit, settings().page_size)])
    rows = await connection.fetch(
        base + "SELECT path,payload,context FROM filtered "
        f"ORDER BY array_position($3::text[],path),ordinal OFFSET ${len(arguments)-1} LIMIT ${len(arguments)}",
        *arguments,
    )
    size = len(json.dumps(result).encode()) + 2048
    for row in rows:
        item = {"path": row["path"], "value": json.loads(row["payload"]), "context": json.loads(row["context"])}
        encoded = len(json.dumps(item, ensure_ascii=False).encode())
        if size + encoded > settings().response_bytes:
            if not result["items"]:
                raise AssessmentLimitError("A report row exceeds the response budget.")
            break
        result["items"].append(item)
        size += encoded
    last = query.offset + len(result["items"])
    result["nextOffset"] = last if last < result["total"] else None
    return result


async def hierarchy(connection: asyncpg.Connection, tenant: str, snapshot_id: str, query: HierarchyQuery) -> dict:
    arguments = [tenant, snapshot_id]
    where = ["r.tenant_id=$1", "r.snapshot_id=$2", "r.path='costHierarchy'", "s.published"]
    if query.subscription_id:
        where.append(f"r.subscription_id={_parameter(arguments, query.subscription_id.lower())}")
    if query.resource_group:
        where.append(f"r.payload->>'resourceGroup'={_parameter(arguments, query.resource_group)}")
    field = "subscriptionId" if not query.subscription_id else "resourceGroup" if not query.resource_group else "resourceId"
    cte = (
        "WITH selected AS (SELECT r.* FROM assessments.records r JOIN assessments.snapshots s "
        "USING(tenant_id,snapshot_id) WHERE " + " AND ".join(where) + "), nodes AS ("
        f"SELECT payload->>'{field}' AS key,sum((payload->>'monthlySpend')::double precision) AS spend,"
        "max(ordinal) AS last_ordinal,min(ordinal) AS first_ordinal FROM selected "
        "GROUP BY 1 HAVING sum((payload->>'monthlySpend')::double precision)>0) "
    )
    stats = await connection.fetchrow(
        cte + "SELECT count(*) AS total,coalesce(sum(spend),0) AS attributed,"
        "(SELECT count(*) FROM selected) AS records,"
        "(SELECT payload->>'subscriptionName' FROM selected ORDER BY ordinal LIMIT 1) AS subscription_name FROM nodes",
        *arguments,
    )
    rows = await connection.fetch(
        cte + "SELECT n.*,r.payload FROM nodes n JOIN selected r ON r.ordinal=n.last_ordinal "
        "ORDER BY spend DESC,first_ordinal LIMIT 24", *arguments,
    )
    items = []
    for row in rows:
        metadata = json.loads(row["payload"])
        items.append({
            "key": row["key"],
            "label": metadata["subscriptionName"] if not query.subscription_id else metadata["resourceGroup"] if not query.resource_group else metadata["resourceName"],
            "detail": "Subscription" if not query.subscription_id else "Resource group" if not query.resource_group else metadata["resourceType"],
            "spend": row["spend"], "resource": metadata if query.resource_group else None,
        })
    return {
        "items": items, "total": stats["total"], "sourceCount": stats["records"],
        "attributedTotal": stats["attributed"], "subscriptionName": stats["subscription_name"],
    }
