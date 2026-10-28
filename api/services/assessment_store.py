"""Private PostgreSQL persistence, token-authenticated pools, and durable job leases."""

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
from collections.abc import Iterator
from datetime import date, datetime
from uuid import UUID, uuid4

import asyncpg

from services import assessment_queries, report_snapshots, runtime_identity
from services.assessment_limits import AssessmentLimitError
from services.assessment_settings import settings
from services.assessment_request_context import dashboard_read
from reports.models import ReportSnapshot
from reports.chat_evidence import ChatEvidence, CHAT_EVIDENCE_BYTES, build_chat_evidence

logger = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None
_read_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None
_credential = None
TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


class QueueFullError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


class DatabaseConfigurationError(RuntimeError):
    pass


async def _password() -> str:
    global _credential
    if _credential is None:
        _credential = runtime_identity.async_credential()
    return (await _credential.get_token(TOKEN_SCOPE)).token


async def pool(*, for_read: bool | None = None) -> asyncpg.Pool:
    global _pool, _read_pool, _pool_lock
    api = os.environ.get("ASSESSMENT_DB_USER") == "meghkosha_api"
    use_read = api and (dashboard_read.get() if for_read is None else for_read)
    existing = _read_pool if use_read else _pool
    if existing is not None:
        return existing
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        existing = _read_pool if use_read else _pool
        if existing is None:
            required = ("ASSESSMENT_DB_HOST", "ASSESSMENT_DB_NAME", "ASSESSMENT_DB_USER")
            if any(not os.environ.get(key) for key in required):
                raise DatabaseConfigurationError("Private assessment database is not configured; complete provisioning/bootstrap.")
            capacity = settings().pool_size
            if api:
                capacity = settings().read_pool_size if use_read else capacity - settings().read_pool_size
            existing = await asyncpg.create_pool(
                host=os.environ["ASSESSMENT_DB_HOST"],
                database=os.environ["ASSESSMENT_DB_NAME"],
                user=os.environ["ASSESSMENT_DB_USER"],
                password=_password,
                ssl=ssl.create_default_context(),
                min_size=0,
                max_size=capacity,
                timeout=10,
                command_timeout=15,
                max_inactive_connection_lifetime=300,
                server_settings={"statement_timeout": "15000", "lock_timeout": "3000",
                                 "application_name": "meghkosha-assessments"},
            )
            if use_read:
                _read_pool = existing
            else:
                _pool = existing
    return existing


async def close() -> None:
    global _pool, _read_pool, _pool_lock, _credential
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _read_pool is not None:
        await _read_pool.close()
        _read_pool = None
    _pool_lock = None
    if _credential is not None:
        await _credential.close()
        _credential = None


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _job(row: asyncpg.Record) -> dict:
    return {
        "jobId": str(row["job_id"]), "subscriptionIds": json.loads(row["subscription_ids"]),
        "staleDays": row["stale_days"], "status": row["status"], "phase": row["phase"],
        "createdAt": row["created_at"].isoformat(), "attempts": row["attempts"],
        "snapshotId": row["snapshot_id"], "errorCode": row["error_code"],
        "error": row["error_message"],
        "taskKind": row.get("task_kind", "assessment"),
    }


async def enqueue(tenant_id: str, actor: str, subscription_ids: list[str], stale_days: int) -> dict:
    ids = report_snapshots.normalize_subscription_ids(subscription_ids)
    if len(ids) > settings().max_subscriptions:
        raise AssessmentLimitError("Too many subscriptions for one assessment. Select a smaller scope.")
    scope = report_snapshots.scope_hash(ids, stale_days)
    db = await pool()
    async with db.acquire(timeout=10) as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(74027261)")
        existing = await connection.fetchrow(
            "SELECT * FROM assessments.jobs WHERE tenant_id=$1 AND scope_hash=$2 "
            "AND status IN ('queued','running')", tenant_id, scope,
        )
        if existing:
            return _job(existing)
        count = await connection.fetchval(
            "SELECT count(*) FROM assessments.jobs WHERE status IN ('queued','running')"
        )
        if count >= settings().queue_capacity:
            raise QueueFullError("Assessment queue is full. Existing reports remain available; retry later.")
        row = await connection.fetchrow(
            "INSERT INTO assessments.jobs "
            "(job_id,tenant_id,requested_by,scope_hash,subscription_ids,stale_days,status) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6,'queued') RETURNING *",
            uuid4(), tenant_id, actor, scope, _json(ids), stale_days,
        )
    return _job(row)


async def get_job(tenant_id: str, job_id: UUID) -> dict | None:
    row = await (await pool()).fetchrow(
        "SELECT * FROM assessments.jobs WHERE tenant_id=$1 AND job_id=$2", tenant_id, job_id,
    )
    return _job(row) if row else None


async def latest_job(tenant_id: str, subscription_ids: list[str], stale_days: int) -> dict | None:
    row = await (await pool()).fetchrow(
        "SELECT * FROM assessments.jobs WHERE tenant_id=$1 AND scope_hash=$2 ORDER BY created_at DESC LIMIT 1",
        tenant_id, report_snapshots.scope_hash(subscription_ids, stale_days),
    )
    return _job(row) if row else None


async def archive_metadata(tenant: str, *, snapshot_id: str | None = None, scope: str | None = None):
    if snapshot_id is None and scope is None:
        raise ValueError("An exact assessment scope or snapshot ID is required.")
    selector = "snapshot_id=$2" if snapshot_id is not None else "scope_hash=$2"
    return await (await pool()).fetchrow(
        "SELECT blob_name,snapshot_id,subscription_ids FROM assessments.snapshots "
        f"WHERE tenant_id=$1 AND published AND {selector} ORDER BY created_at DESC LIMIT 1",
        tenant, snapshot_id if snapshot_id is not None else scope,
    )


async def snapshot_catalog(tenant: str, subscription_ids: list[str], limit: int) -> list[dict]:
    scopes = [report_snapshots.scope_hash(subscription_ids, days) for days in (7, 14, 30, 60, 90, 180, 365)]
    rows = await (await pool()).fetch(
        "SELECT s.* FROM unnest($2::text[]) AS requested(scope) CROSS JOIN LATERAL ("
        "SELECT snapshot_id,scope_hash,subscription_ids,stale_days,created_at,"
        "summary->'reportMetadata' AS metadata FROM assessments.snapshots "
        "WHERE tenant_id=$1 AND published AND scope_hash=requested.scope "
        "AND subscription_ids=$4::jsonb ORDER BY created_at DESC LIMIT $3) s "
        "ORDER BY s.created_at DESC LIMIT $3",
        tenant, scopes, min(max(limit, 1), 100),
        _json(report_snapshots.normalize_subscription_ids(subscription_ids)),
    )
    result = []
    for row in rows:
        metadata = json.loads(row["metadata"])
        result.append({
            "snapshotId": row["snapshot_id"], "scopeHash": row["scope_hash"],
            "subscriptionIds": json.loads(row["subscription_ids"]), "staleDays": row["stale_days"],
            "createdAt": row["created_at"].isoformat(), "period": metadata["period"],
            "periodStart": metadata["periodStart"], "periodEnd": metadata["periodEnd"],
            "currency": metadata["currency"], "costBasis": metadata["costBasis"],
            "reportSchemaVersion": report_snapshots.REPORT_SCHEMA_VERSION,
        })
    return result


async def cancel(tenant_id: str, job_id: UUID, actor: str) -> bool:
    result = await (await pool()).execute(
        "UPDATE assessments.jobs SET status='cancelled',phase='cancelled',completed_at=now() "
        "WHERE tenant_id=$1 AND job_id=$2 AND requested_by=$3 AND status IN ('queued','running')",
        tenant_id, job_id, actor,
    )
    return result == "UPDATE 1"


async def azure_request_delay(key: str, interval: float) -> float:
    db = await pool()
    async with db.acquire(timeout=10) as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO assessments.azure_request_budgets VALUES ($1,clock_timestamp()) "
            "ON CONFLICT DO NOTHING", key,
        )
        delay = await connection.fetchval(
            "SELECT greatest(0,extract(epoch FROM next_at-clock_timestamp())) "
            "FROM assessments.azure_request_budgets WHERE scope_key=$1 FOR UPDATE", key,
        )
        if delay <= 0:
            await connection.execute(
                "UPDATE assessments.azure_request_budgets SET next_at=clock_timestamp()+make_interval(secs=>$2) "
                "WHERE scope_key=$1", key, interval,
            )
        return float(delay)


async def defer_azure_requests(key: str, delay: float) -> None:
    await (await pool()).execute(
        "INSERT INTO assessments.azure_request_budgets VALUES ($1,clock_timestamp()+make_interval(secs=>$2)) "
        "ON CONFLICT (scope_key) DO UPDATE SET next_at=greatest("
        "assessments.azure_request_budgets.next_at,excluded.next_at)", key, delay,
    )


async def claim() -> asyncpg.Record | None:
    db = await pool()
    async with db.acquire(timeout=10) as connection, connection.transaction():
        await connection.execute(
            "UPDATE assessments.jobs SET status='failed',phase='failed',completed_at=now(),"
            "error_code='worker_attempts_exhausted',error_message='Worker stopped repeatedly. "
            "Review worker logs/capacity before starting another assessment.' "
            "WHERE status='running' AND lease_until < now() AND attempts >= $1",
            settings().max_attempts,
        )
        return await connection.fetchrow(
            "WITH candidate AS (SELECT job_id FROM assessments.jobs "
            "WHERE (status='queued' OR (status='running' AND lease_until < now())) "
            "AND next_attempt_at <= now() AND attempts < $1 ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE assessments.jobs j SET status='running',phase='collecting',"
            "started_at=coalesce(started_at,now()),lease_token=$2,"
            "lease_until=now()+make_interval(secs=>$3),attempts=attempts+1 "
            "FROM candidate c WHERE j.job_id=c.job_id RETURNING j.*",
            settings().max_attempts, uuid4(), settings().lease_seconds,
        )


async def heartbeat(job_id: UUID, token: UUID) -> bool:
    result = await (await pool()).execute(
        "UPDATE assessments.jobs SET lease_until=now()+make_interval(secs=>$3) "
        "WHERE job_id=$1 AND lease_token=$2 AND status='running' AND lease_until>now()",
        job_id, token, settings().lease_seconds,
    )
    return result == "UPDATE 1"


async def fail(job_id: UUID, token: UUID, message: str, *, retryable: bool, code: str) -> None:
    result = await (await pool()).execute(
        "UPDATE assessments.jobs SET status=CASE WHEN $3 AND attempts<$4 THEN 'queued' ELSE 'failed' END,"
        "phase=CASE WHEN $3 AND attempts<$4 THEN 'retrying' ELSE 'failed' END,"
        "next_attempt_at=now()+make_interval(secs=>least(300,attempts*30)),"
        "completed_at=CASE WHEN $3 AND attempts<$4 THEN NULL ELSE now() END,"
        "error_message=$5,error_code=$6,lease_until=NULL,lease_token=NULL "
        "WHERE job_id=$1 AND lease_token=$2 AND status='running'",
        job_id, token, retryable, settings().max_attempts, message[:1000], code,
    )
    if result != "UPDATE 1":
        logger.info("Assessment %s no longer owns its job lease", job_id)


def _resource_collection(path: str) -> bool:
    return path in {
        "costHierarchy", "costDetails.rows", "storageOptimization.accounts", "aiUsage.deployments",
        "aiUsage.opportunities", "extendedSupport.rows", "offHoursSavings.rows", "chargeback.rows",
    } or bool(re.fullmatch(r"(?:domains\.[^.]+\.categories\.\d+|tierACategories\.\d+)\.lines", path)) \
        or bool(re.fullmatch(r"tagCosts\.dimensions\.\d+\.rows", path)) \
        or path.startswith("domains.") and path.endswith(".advisorRecommendations")


def _metadata_collection(path: str) -> bool:
    return path in {
        "tierACategories", "tagCosts.dimensions", "prioritizedFindings", "savingsRoadmap", "actionPlan",
        "subscriptionBreakdown", "governance", "topServices", "spendCategories", "regionSpend",
        "networkMetricCoverage", "advisorReconciliation.measures", "spendHistory.months",
        "commitmentInsights.months", "pricingSummary.subscriptions", "dailyCostTrend.days",
        "tagDailyCostTrend.windowDates",
    } or bool(re.fullmatch(r"domains\.[^.]+\.categories", path))


def collection_contexts(report: dict, paths: set[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}

    def visit(value, path: str, context: dict[str, str]):
        if isinstance(value, dict):
            context = {**context, **{key: value[key] for key in
                       ("domain", "category", "displayName", "impactType", "tagKey") if isinstance(value.get(key), str)}}
        if path in paths:
            result[path] = context
            if _resource_collection(path):
                return
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else key, context)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}.{index}", context)

    visit(report, "", {})
    return result


def split_report(report: dict) -> tuple[dict, list[tuple[str, list]]]:
    collections: list[tuple[str, list]] = []

    def visit(value, path: str):
        if isinstance(value, dict):
            return {key: visit(item, f"{path}.{key}" if path else key) for key, item in value.items()}
        if path == "costDetails.dates" and isinstance(value, list):
            if len(value) > 366:
                raise AssessmentLimitError("Cost evidence exceeds the supported 366-day date axis.")
            return value
        if isinstance(value, list) and _metadata_collection(path):
            return [visit(item, f"{path}.{index}") for index, item in enumerate(value)]
        if isinstance(value, list) and (_resource_collection(path) or len(value) > settings().inline_rows):
            records = [
                item if _resource_collection(path) and len(_json(item).encode()) <= settings().record_bytes
                else visit(item, f"{path}.{index}")
                for index, item in enumerate(value)
            ]
            collections.append((path, records))
            return []
        if isinstance(value, list):
            return [visit(item, f"{path}.{index}") for index, item in enumerate(value)]
        return value

    summary = visit(report, "")
    if len(_json(summary).encode()) > settings().response_bytes // 2:
        raise AssessmentLimitError("Assessment summary exceeds the response budget. Reduce the inline-row setting.")
    manifest = [{"path": path, "total": len(records)} for path, records in collections]
    if len(_json(manifest).encode()) > settings().response_bytes // 4:
        raise AssessmentLimitError("Assessment has too many nested collections for one dashboard. Narrow its scope.")
    return summary, collections


def interactive_row_count(view: dict) -> int:
    def count(value) -> int:
        if isinstance(value, dict):
            return sum(count(item) for item in value.values())
        if isinstance(value, list):
            return len(value) + sum(count(item) for item in value)
        return 0
    return count(view["report"]) + sum(item["total"] for item in view["collections"])


def record_rows(tenant: str, snapshot_id: str, path: str, records: list) -> Iterator[tuple]:
    for ordinal, record in enumerate(records):
        payload = _json(record)
        size = len(payload.encode())
        if size > settings().record_bytes:
            raise AssessmentLimitError(f"A {path} record exceeds the configured detail-record budget.")
        fields = record if isinstance(record, dict) else {}
        subscription_id = str(fields.get("subscriptionId") or "").lower()
        resource_id = str(fields.get("resourceId") or fields.get("id") or "").lower()
        search = " ".join(str(fields.get(key) or "") for key in
                          ("resourceName", "name", "category", "resourceType", "resourceGroup"))[:2000].lower()
        yield tenant, snapshot_id, path, ordinal, subscription_id, resource_id, search, payload, size


async def publish(snapshot: ReportSnapshot, job: asyncpg.Record, blob_name: str) -> None:
    if (snapshot.scope_hash != job["scope_hash"] or snapshot.stale_days != job["stale_days"]
            or snapshot.subscription_ids != json.loads(job["subscription_ids"])
            or snapshot.report.report_metadata.stale_days != snapshot.stale_days
            or not snapshot.report.completeness.complete):
        raise ValueError("Completed assessment does not match its leased job scope.")
    complete = snapshot.report.model_dump(by_alias=True, mode="json")
    summary, collections = split_report(complete)
    contexts = collection_contexts(complete, {path for path, _ in collections})
    if len(_json(contexts).encode()) > settings().response_bytes // 4:
        raise AssessmentLimitError("Report collection metadata exceeds its response budget.")
    chat = _json(build_chat_evidence(snapshot.report).model_dump(by_alias=True, mode="json"))
    if len(chat.encode()) > CHAT_EVIDENCE_BYTES:
        logger.warning("Chat evidence exceeds its bounded payload budget for snapshot %s; report publication remains complete",
                       snapshot.snapshot_id)
        chat = None
    tenant = job["tenant_id"]
    anomaly = snapshot.report._anomaly_evidence
    anomaly_content = anomaly.model_dump_json(by_alias=True).encode() if anomaly is not None else None
    db = await pool()
    async with db.acquire(timeout=10) as connection, connection.transaction():
        await connection.execute("SET LOCAL statement_timeout='5min'")
        await connection.execute(
            "INSERT INTO assessments.snapshots "
            "(tenant_id,snapshot_id,scope_hash,subscription_ids,stale_days,created_at,summary,blob_name,period_start,chat_evidence,collection_context) "
            "VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8,$9,$10::jsonb,$11::jsonb)",
            tenant, snapshot.snapshot_id, snapshot.scope_hash, _json(snapshot.subscription_ids),
            snapshot.stale_days, datetime.fromisoformat(snapshot.created_at), _json(summary), blob_name,
            snapshot.report.report_metadata.period_start,
            chat, _json(contexts),
        )
        await assessment_queries.persist_costs(connection, tenant, snapshot.snapshot_id, snapshot.report.cost_details)
        if anomaly_content is not None:
            parts = (len(anomaly_content) + 1048575) // 1048576
            for ordinal in range(parts):
                await connection.execute(
                    "INSERT INTO assessments.anomaly_parts VALUES($1,$2,$3,$4)",
                    tenant, snapshot.snapshot_id, ordinal, anomaly_content[ordinal * 1048576:(ordinal + 1) * 1048576],
                )
            await connection.execute(
                "UPDATE assessments.snapshots SET anomaly_metadata=$3::jsonb WHERE tenant_id=$1 AND snapshot_id=$2",
                tenant, snapshot.snapshot_id, _json({
                    "parts": parts, "bytes": len(anomaly_content), "sha256": hashlib.sha256(anomaly_content).hexdigest(),
                }),
            )
        for path, records in collections:
            await connection.execute(
                "INSERT INTO assessments.collections VALUES ($1,$2,$3,$4)",
                tenant, snapshot.snapshot_id, path, len(records),
            )
            batch = []
            for record in record_rows(tenant, snapshot.snapshot_id, path, records):
                batch.append(record)
                if len(batch) == 100:
                    await connection.executemany(
                        "INSERT INTO assessments.records VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)", batch,
                    )
                    batch.clear()
            if batch:
                await connection.executemany(
                    "INSERT INTO assessments.records VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)", batch,
                )
        await connection.execute("SELECT assessments.refresh_serving_statistics(true)")
        result = await connection.execute(
            "UPDATE assessments.jobs SET status='succeeded',phase='complete',completed_at=now(),"
            "snapshot_id=$3,error_code=NULL,error_message=NULL WHERE job_id=$1 AND lease_token=$2 "
            "AND status='running' AND lease_until>clock_timestamp()",
            job["job_id"], job["lease_token"], snapshot.snapshot_id,
        )
        if result != "UPDATE 1":
            raise LeaseLostError("Assessment was cancelled or its lease expired before publication.")
        await connection.execute(
            "UPDATE assessments.snapshots SET published=true,serving_version=1 WHERE tenant_id=$1 AND snapshot_id=$2",
            tenant, snapshot.snapshot_id,
        )


async def snapshot_view(tenant: str, *, scope: str | None = None, snapshot_id: str | None = None,
                        before_period: str | None = None) -> dict | None:
    db = await pool()
    if scope is None and snapshot_id is None:
        raise ValueError("An exact assessment scope or snapshot ID is required.")
    selector = "snapshot_id=$2" if snapshot_id is not None else "scope_hash=$2"
    period_clause = "AND period_start<$3 " if before_period is not None else ""
    arguments = [tenant, snapshot_id if snapshot_id is not None else scope]
    if before_period is not None:
        arguments.append(before_period)
    row = await db.fetchrow(
        "SELECT snapshot_id,scope_hash,subscription_ids,stale_days,created_at,summary,"
        "chat_evidence IS NOT NULL AS chat_available,serving_version,collection_context FROM assessments.snapshots WHERE tenant_id=$1 AND published "
        f"AND {selector} {period_clause}ORDER BY created_at DESC LIMIT 1", *arguments,
    )
    if not row:
        return None
    collections = await db.fetch(
        "SELECT path,record_count FROM assessments.collections WHERE tenant_id=$1 AND snapshot_id=$2 ORDER BY path",
        tenant, row["snapshot_id"],
    )
    contexts = json.loads(row["collection_context"])
    view = {
        "snapshotId": row["snapshot_id"], "scopeHash": row["scope_hash"],
        "subscriptionIds": json.loads(row["subscription_ids"]), "staleDays": row["stale_days"],
        "createdAt": row["created_at"].isoformat(), "report": json.loads(row["summary"]),
        "reportSchemaVersion": report_snapshots.REPORT_SCHEMA_VERSION,
        "servingVersion": row["serving_version"],
        "chatAvailable": row["chat_available"],
        "collections": [{"path": item["path"], "total": item["record_count"],
                         **({"context": contexts[item["path"]]} if contexts.get(item["path"]) else {})} for item in collections],
    }
    if len(_json(view).encode()) > settings().response_bytes:
        raise AssessmentLimitError("Stored dashboard exceeds its response budget; ask the operator to rebuild it.")
    return view


async def chat_evidence(tenant: str, snapshot_id: str, subscription_ids: list[str]) -> ChatEvidence | None:
    payload = await (await pool()).fetchval(
        "SELECT CASE WHEN octet_length(chat_evidence::text)<=$4 THEN chat_evidence END "
        "FROM assessments.snapshots WHERE tenant_id=$1 AND snapshot_id=$2 AND published "
        "AND subscription_ids=$3::jsonb",
        tenant, snapshot_id, _json(report_snapshots.normalize_subscription_ids(subscription_ids)), CHAT_EVIDENCE_BYTES * 2,
    )
    if payload is None:
        return None
    evidence = ChatEvidence.model_validate_json(payload)
    if not evidence.completeness.complete or sorted(row.subscription_id.lower() for row in evidence.subscription_breakdown) != sorted(
        value.lower() for value in subscription_ids
    ):
        raise ValueError("Stored Chat evidence does not match the authorized completed assessment scope.")
    return evidence


async def page(tenant: str, snapshot_id: str, path: str, after: int, limit: int,
               subscription_id: str | None, search: str) -> dict | None:
    db = await pool()
    total = await db.fetchval(
        "SELECT c.record_count FROM assessments.collections c JOIN assessments.snapshots s "
        "USING (tenant_id,snapshot_id) WHERE c.tenant_id=$1 AND c.snapshot_id=$2 AND c.path=$3 AND s.published",
        tenant, snapshot_id, path,
    )
    if total is None:
        return None
    pattern = "%" + search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    arguments = [tenant, snapshot_id, path, after]
    predicate = "tenant_id=$1 AND snapshot_id=$2 AND path=$3 AND ordinal>$4"
    if subscription_id is not None:
        arguments.append(subscription_id)
        predicate += f" AND subscription_id=${len(arguments)}"
    if search:
        arguments.append(pattern)
        predicate += f" AND search_text ILIKE ${len(arguments)}"
    limit_parameter = len(arguments) + 1
    bytes_parameter = len(arguments) + 2
    rows = await db.fetch(
        "WITH candidates AS (SELECT ordinal,payload,payload_bytes FROM assessments.records "
        f"WHERE {predicate} ORDER BY ordinal LIMIT ${limit_parameter}), sized AS (SELECT *,"
        "sum(payload_bytes+64) OVER (ORDER BY ordinal) AS bytes FROM candidates) "
        f"SELECT ordinal,payload FROM sized WHERE bytes<=${bytes_parameter} ORDER BY ordinal",
        *arguments, limit, settings().response_bytes - 2048,
    )
    last = rows[-1]["ordinal"] if rows else after
    arguments[3] = last
    more = await db.fetchval(
        f"SELECT EXISTS(SELECT 1 FROM assessments.records WHERE {predicate})", *arguments,
    )
    return {"path": path, "total": total, "items": [json.loads(row["payload"]) for row in rows],
            "nextCursor": last if more else None, "filtered": bool(subscription_id or search)}


async def sql_snapshot(tenant: str, snapshot_id: str, *, complete: bool = False) -> ReportSnapshot:
    view = await snapshot_view(tenant, snapshot_id=snapshot_id)
    if view is None:
        raise report_snapshots.ReportSnapshotNotFoundError(snapshot_id)
    if complete:
        if os.environ.get("ASSESSMENT_WORKER_PROCESS") != "true":
            raise AssessmentLimitError("Complete report materialization is background-worker only.")
        async with (await pool()).acquire(timeout=10) as connection, connection.transaction(isolation="repeatable_read", readonly=True):
            for collection in sorted(view["collections"], key=lambda item: item["path"].count(".")):
                values = []
                last = -1
                while len(values) < collection["total"]:
                    rows = await connection.fetch(
                        "SELECT ordinal,payload FROM assessments.records WHERE tenant_id=$1 AND snapshot_id=$2 "
                        "AND path=$3 AND ordinal>$4 ORDER BY ordinal LIMIT 100",
                        tenant, snapshot_id, collection["path"], last,
                    )
                    if not rows:
                        raise report_snapshots.ReportSnapshotIntegrityError("Stored report collection is incomplete.")
                    values.extend(json.loads(row["payload"]) for row in rows)
                    last = rows[-1]["ordinal"]
                if len(values) != collection["total"]:
                    raise report_snapshots.ReportSnapshotIntegrityError("Stored report collection count is invalid.")
                target = view["report"]
                pieces = collection["path"].split(".")
                try:
                    for piece in pieces[:-1]:
                        target = target[int(piece)] if isinstance(target, list) else target[piece]
                    if isinstance(target, list):
                        target[int(pieces[-1])] = values
                    else:
                        target[pieces[-1]] = values
                except (KeyError, IndexError, ValueError, TypeError) as error:
                    raise report_snapshots.ReportSnapshotIntegrityError("Stored report collection path is invalid.") from error
    try:
        return ReportSnapshot.model_validate(view)
    except ValueError as error:
        raise report_snapshots.ReportSnapshotIntegrityError("Stored SQL report failed schema validation.") from error


async def enqueue_artifact(tenant: str, actor: str, subscription_ids: list[str], stale_days: int, payload: dict) -> dict:
    scope = hashlib.sha256(("artifact:" + json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)).encode()).hexdigest()
    async with (await pool()).acquire(timeout=10) as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(74027261)")
        existing = await connection.fetchrow(
            "SELECT * FROM assessments.jobs WHERE tenant_id=$1 AND scope_hash=$2 AND status IN ('queued','running')",
            tenant, scope,
        )
        if existing:
            return _job(existing)
        count = await connection.fetchval("SELECT count(*) FROM assessments.jobs WHERE status IN ('queued','running')")
        if count >= settings().queue_capacity:
            raise QueueFullError("Report processing queue is full. Retry shortly.")
        row = await connection.fetchrow(
            "INSERT INTO assessments.jobs(job_id,tenant_id,requested_by,scope_hash,subscription_ids,stale_days,status,task_kind,task_payload) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6,'queued','artifact',$7::jsonb) RETURNING *",
            uuid4(), tenant, actor, scope, _json(report_snapshots.normalize_subscription_ids(subscription_ids)), stale_days, _json(payload),
        )
    return _job(row)


async def publish_artifact(job, file_name: str, media_type: str, content: bytes) -> None:
    if len(content) > settings().spool_bytes:
        raise AssessmentLimitError("Generated report exceeds the worker output budget.")
    parts = (len(content) + 1048575) // 1048576
    metadata = _json({
        "fileName": file_name, "mediaType": media_type, "bytes": len(content), "parts": parts,
        "sha256": hashlib.sha256(content).hexdigest(),
    })
    async with (await pool()).acquire(timeout=10) as connection, connection.transaction():
        await connection.execute("SET LOCAL statement_timeout='5min'")
        for ordinal in range(parts):
            await connection.execute(
                "INSERT INTO assessments.artifact_parts VALUES($1,$2,$3,$4)",
                job["tenant_id"], job["job_id"], ordinal, content[ordinal * 1048576:(ordinal + 1) * 1048576],
            )
        result = await connection.execute(
            "UPDATE assessments.jobs SET status='succeeded',phase='complete',completed_at=now(),result_metadata=$3::jsonb "
            "WHERE job_id=$1 AND lease_token=$2 AND status='running' AND lease_until>clock_timestamp()",
            job["job_id"], job["lease_token"], metadata,
        )
        if result != "UPDATE 1":
            raise LeaseLostError("Report generation lost its job lease before publication.")


async def artifact_metadata(tenant: str, job_id: UUID) -> dict | None:
    payload = await (await pool()).fetchval(
        "SELECT result_metadata FROM assessments.jobs WHERE tenant_id=$1 AND job_id=$2 "
        "AND task_kind='artifact' AND status='succeeded'", tenant, job_id,
    )
    return json.loads(payload) if payload is not None else None


async def stream_artifact(tenant: str, job_id: UUID, metadata: dict):
    received = 0
    digest = hashlib.sha256()
    for ordinal in range(metadata["parts"]):
        content = await (await pool()).fetchval(
            "SELECT content FROM assessments.artifact_parts WHERE tenant_id=$1 AND job_id=$2 AND ordinal=$3",
            tenant, job_id, ordinal,
        )
        if content is None:
            logger.error("Stored artifact %s is missing part %s", job_id, ordinal)
            raise report_snapshots.ReportSnapshotIntegrityError("Stored report download is incomplete.")
        received += len(content)
        digest.update(content)
        yield content
    if received != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
        logger.error("Stored artifact %s failed download integrity validation", job_id)
        raise report_snapshots.ReportSnapshotIntegrityError("Stored report download failed integrity validation.")


async def anomaly_metadata(tenant: str, snapshot_id: str) -> dict | None:
    value = await (await pool()).fetchval(
        "SELECT anomaly_metadata FROM assessments.snapshots WHERE tenant_id=$1 AND snapshot_id=$2 AND published",
        tenant, snapshot_id,
    )
    return json.loads(value) if value is not None else None


async def resource_cost_evidence(
    tenant: str, snapshot_id: str, resource_ids: list[str], start: date, end: date,
) -> dict[str, dict]:
    result = {}
    identifiers = sorted({value.lower() for value in resource_ids})
    for offset in range(0, len(identifiers), 250):
        rows = await (await pool()).fetch(
            """WITH daily AS (
                SELECT r.resource_id,true AS has_daily,coalesce(sum(d.amount),0) AS amount
                FROM assessments.cost_rows r LEFT JOIN assessments.cost_daily d
                ON (r.tenant_id,r.snapshot_id,r.detail_id)=(d.tenant_id,d.snapshot_id,d.detail_id)
                AND d.charge_date BETWEEN $4 AND $5
                WHERE r.tenant_id=$1 AND r.snapshot_id=$2 AND r.resource_id=ANY($3::text[])
                GROUP BY r.resource_id
            ), monthly AS (
                SELECT resource_id,sum((payload->>'monthlySpend')::double precision) AS amount
                FROM assessments.records
                WHERE tenant_id=$1 AND snapshot_id=$2 AND path='costHierarchy' AND resource_id=ANY($3::text[])
                GROUP BY resource_id
            )
            SELECT wanted.id AS resource_id,coalesce(d.has_daily,false) AS has_daily,d.amount AS daily,m.amount AS monthly
            FROM unnest($3::text[]) wanted(id)
            LEFT JOIN daily d ON d.resource_id=wanted.id LEFT JOIN monthly m ON m.resource_id=wanted.id""",
            tenant, snapshot_id, identifiers[offset:offset + 250], start, end,
        )
        result.update((row["resource_id"], dict(row)) for row in rows)
    return result


async def stream_anomalies(tenant: str, snapshot_id: str, metadata: dict):
    digest = hashlib.sha256()
    received = 0
    for ordinal in range(metadata["parts"]):
        content = await (await pool()).fetchval(
            "SELECT content FROM assessments.anomaly_parts WHERE tenant_id=$1 AND snapshot_id=$2 AND ordinal=$3",
            tenant, snapshot_id, ordinal,
        )
        if content is None:
            logger.error("Stored anomaly evidence %s is missing part %s", snapshot_id, ordinal)
            raise report_snapshots.ReportSnapshotIntegrityError("Stored anomaly evidence is incomplete.")
        received += len(content)
        digest.update(content)
        yield content
    if received != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
        logger.error("Stored anomaly evidence %s failed integrity validation", snapshot_id)
        raise report_snapshots.ReportSnapshotIntegrityError("Stored anomaly evidence failed integrity validation.")
