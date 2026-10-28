import asyncio
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

import main
from jobs import report_artifact
from jobs.assessment_bootstrap import RUNTIME_GRANTS
from reports.models import CostDetailSummary
from services import assessment_control, assessment_store, finops_actions, report_snapshots
from services.assessment_settings import settings
from test_assessment_query_postgres import RESOURCE, SNAPSHOT, SUB, TENANT, connect, detail
from test_scalable_assessments import make_report

pytestmark = pytest.mark.enable_socket


async def database(monkeypatch):
    seed = await connect()
    dsn = os.environ["ASSESSMENT_TEST_DSN"]
    await seed.close()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)

    async def get_pool(**kwargs):
        return pool

    monkeypatch.setattr(assessment_store, "pool", get_pool)
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    settings.cache_clear()
    return pool


def test_real_sql_snapshot_roundtrip_never_reads_blob(monkeypatch):
    async def run():
        db = await database(monkeypatch)
        try:
            await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
            job = await assessment_store.enqueue(TENANT, "synthetic-actor", [SUB], 90)
            claimed = await assessment_store.claim()
            snapshot = report_snapshots.build_snapshot(make_report(50), [SUB], 90)
            await assessment_store.publish(snapshot, claimed, "archive-only.json")

            def no_blob():
                pytest.fail("Normal SQL snapshot serving must never open Blob Storage.")

            monkeypatch.setattr(report_snapshots, "get_container_client", no_blob)
            metadata = await report_snapshots.load_snapshot(snapshot.snapshot_id)
            assert metadata.snapshot_id == snapshot.snapshot_id and metadata.report.cost_hierarchy == []
            view = await assessment_store.snapshot_view(TENANT, snapshot_id=snapshot.snapshot_id)
            assert view["servingVersion"] == 1
            assert next(item["total"] for item in view["collections"] if item["path"] == "costHierarchy") == 50
            monkeypatch.setenv("ASSESSMENT_WORKER_PROCESS", "true")
            full = await report_snapshots.load_snapshot(snapshot.snapshot_id)
            assert full.report.model_dump() == snapshot.report.model_dump()
            assert (await assessment_store.get_job(TENANT, uuid4())) is None
            assert (await assessment_store.get_job(TENANT, UUID(job["jobId"])))["status"] == "succeeded"
        finally:
            await db.close()
            settings.cache_clear()
    asyncio.run(run())


def test_real_artifact_publication_is_fenced_chunked_and_tenant_scoped(monkeypatch):
    async def run():
        db = await database(monkeypatch)
        try:
            await db.execute("TRUNCATE assessments.jobs CASCADE")
            result = await assessment_store.enqueue_artifact(TENANT, "actor", [SUB], 90, {
                "operation": "report", "snapshotIds": [SNAPSHOT], "reportType": "full",
            })
            claimed = await assessment_store.claim()
            assert claimed["task_kind"] == "artifact"
            content = b"synthetic-output" * 150000
            await assessment_store.publish_artifact(claimed, "report.xlsx", "application/octet-stream", content)
            job_id = UUID(result["jobId"])
            metadata = await assessment_store.artifact_metadata(TENANT, job_id)
            assert metadata["parts"] > 1 and metadata["bytes"] == len(content)
            downloaded = b"".join([part async for part in assessment_store.stream_artifact(TENANT, job_id, metadata)])
            assert downloaded == content
            assert await assessment_store.artifact_metadata("another-tenant", job_id) is None
        finally:
            await db.close()
            settings.cache_clear()
    asyncio.run(run())


def test_real_control_leases_and_action_audit_keep_original_concurrency_rules(monkeypatch):
    async def run():
        db = await database(monkeypatch)
        try:
            await db.execute("TRUNCATE assessments.control_state,assessments.action_events")
            assert await assessment_control.create(TENANT, "test", "one", '{"state":"active"}')
            first, second = uuid4(), uuid4()
            assert await assessment_control.acquire(TENANT, "test", "one", first)
            assert not await assessment_control.acquire(TENANT, "test", "one", second)
            assert not await assessment_control.save(TENANT, "test", "one", second, '{"state":"wrong"}')
            assert await assessment_control.save(TENANT, "test", "one", first, '{"state":"paused"}')
            await assessment_control.release(TENANT, "test", "one", second)
            assert not await assessment_control.acquire(TENANT, "test", "one", second)
            await assessment_control.release(TENANT, "test", "one", first)
            assert await assessment_control.acquire(TENANT, "test", "one", second)

            state = finops_actions.build_state(
                scope_hash="a" * 64, action_id="unattached_disks", status="completed", owner="owner",
                due_date=None, realized_saving_month=5, note="synthetic", actor="actor",
            )
            saved = await finops_actions.save_action(state, expected_version=0)
            assert saved.version == 1
            with pytest.raises(finops_actions.ActionConflictError):
                await finops_actions.save_action(state, expected_version=0)
            repeated = await finops_actions.save_action(state, expected_version=1)
            assert repeated.completed_at == saved.completed_at
            assert [item.version for item in await finops_actions.list_action_history("a" * 64, "unattached_disks")] == [2, 1]
            assert (await finops_actions.list_actions("a" * 64))[0].version == 2
        finally:
            await db.close()
            settings.cache_clear()
    asyncio.run(run())


def test_original_feature_apis_and_downloads_use_sql_not_blob(monkeypatch):
    async def run():
        db = await database(monkeypatch)
        try:
            await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
            dates = [str(date(2026, 6, 1) + timedelta(days=index)) for index in range(61)]
            report = make_report(1).model_copy(update={"cost_details": CostDetailSummary(
                status="complete", dates=dates, rows=[detail("source", costs={"2026-06-01": 5, "2026-07-01": 10})],
            )})
            evidence = main.detect_anomalies([], "USD")
            report._anomaly_evidence = evidence
            snapshot = report_snapshots.build_snapshot(report, [SUB], 90)
            await assessment_store.enqueue(TENANT, "synthetic-actor", [SUB], 90)
            await assessment_store.publish(snapshot, await assessment_store.claim(), "archive-only.json")

            def no_blob():
                pytest.fail("The original dashboard and rendered reports must use SQL, not Blob reads.")

            monkeypatch.setattr(report_snapshots, "get_container_client", no_blob)
            availability = main.resource_uptime.ResourceAvailability(
                resourceId=RESOURCE, date="2026-07-02", status="complete", statusMessage="Synthetic evidence",
                availableHours=24, coverageMinutes=1440,
            )
            get_availability = AsyncMock(return_value=availability)
            monkeypatch.setattr(main.resource_uptime, "get_resource_availability", get_availability)
            monkeypatch.setattr(main.arm_client, "_list_arm_collection", AsyncMock(return_value=[{
                "id": "retirement", "properties": {
                    "resourceMetadata": {"resourceId": RESOURCE},
                    "extendedProperties": {"recommendationSubCategory": "ServiceUpgradeAndRetirement",
                                           "retirementFeatureName": "Synthetic retirement"},
                },
            }]))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                uptime = await client.get("/api/report/resource-availability", params={
                    "snapshotId": snapshot.snapshot_id, "resourceId": RESOURCE, "date": "2026-07-02",
                })
                assert uptime.status_code == 200 and uptime.json()["availableHours"] == 24
                get_availability.assert_awaited_once_with(RESOURCE, date(2026, 7, 2))
                retirement = await client.get("/api/report/retirements", params={"snapshotId": snapshot.snapshot_id})
                assert retirement.status_code == 200, retirement.text
                assert retirement.json()["notices"][0]["matchedCost"] == 10
                anomaly = await client.post("/api/anomalies", json={"subscriptionIds": [SUB], "snapshotId": snapshot.snapshot_id})
                assert anomaly.status_code == 200 and anomaly.json() == evidence.model_dump(by_alias=True, mode="json")
                query = await client.post(f"/api/report/{snapshot.snapshot_id}/cost-query", json={
                    "kind": "metrics", "window": {"startDate": "2026-07-01", "endDate": "2026-07-31"},
                })
                assert query.status_code == 200 and query.json()["currentTotal"] == 10
                assert query.json()["previousTotal"] == 5
                with monkeypatch.context() as cached:
                    cached.setattr(main.assessment_queries, "execute", AsyncMock(side_effect=AssertionError("Immutable query should be cached")))
                    repeated = await client.post(f"/api/report/{snapshot.snapshot_id}/cost-query", json={
                        "kind": "metrics", "window": {"startDate": "2026-07-01", "endDate": "2026-07-31"},
                    })
                    assert repeated.status_code == 200 and repeated.json() == query.json()
                    cached.setattr(main.access_control, "require_all_subscription_access", AsyncMock(
                        side_effect=main.HTTPException(status_code=403, detail="Access revoked"),
                    ))
                    denied = await client.post(f"/api/report/{snapshot.snapshot_id}/cost-query", json={
                        "kind": "metrics", "window": {"startDate": "2026-07-01", "endDate": "2026-07-31"},
                    })
                    assert denied.status_code == 403

                responses = [
                    await client.get("/api/report/export", params={"snapshotId": snapshot.snapshot_id, "type": "full"}),
                    await client.post("/api/report/export/custom", json={"snapshotIds": [snapshot.snapshot_id], "modules": ["summary"]}),
                    await client.post("/api/report/cost-details/export", json={
                        "snapshotId": snapshot.snapshot_id, "startDate": "2026-07-01", "endDate": "2026-07-31",
                        "filters": {},
                    }),
                ]
                for response in responses:
                    assert response.status_code == 202, response.text
                for _ in responses:
                    job = await assessment_store.claim()
                    assert job["task_kind"] == "artifact"
                    with monkeypatch.context() as worker:
                        worker.setenv("ASSESSMENT_WORKER_PROCESS", "true")
                        await report_artifact.run(job)
                    download = await client.get(f"/api/report/artifacts/{job['job_id']}")
                    assert download.status_code == 200, download.text[:300]
                    assert download.content.startswith(b"PK") and int(download.headers["content-length"]) == len(download.content)
        finally:
            await db.close()
            settings.cache_clear()
    asyncio.run(run())


def test_statistics_maintenance_is_worker_only_and_does_not_grant_table_ownership(monkeypatch):
    async def run():
        db = await database(monkeypatch)
        try:
            for role in ("meghkosha_api", "meghkosha_worker"):
                if not await db.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                    await db.execute(f"CREATE ROLE {role}")
            await db.execute(RUNTIME_GRANTS.replace("DATABASE meghkosha", "DATABASE meghkosha_test"))
            async with db.acquire() as connection:
                try:
                    await connection.execute("SET ROLE meghkosha_api")
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute("SELECT assessments.refresh_serving_statistics(false)")
                    await connection.execute("SET ROLE meghkosha_worker")
                    await connection.execute("SELECT assessments.refresh_serving_statistics(true)")
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute("CREATE TABLE assessments.not_allowed(value int)")
                finally:
                    await connection.execute("RESET ROLE")
        finally:
            await db.close()
            settings.cache_clear()
    asyncio.run(run())
