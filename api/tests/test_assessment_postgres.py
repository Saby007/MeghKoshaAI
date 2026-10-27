import asyncio
import gzip
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

import main
from services import assessment_store, focus_cost_reader, report_snapshots
from services.assessment_limits import AssessmentBudget, active_budget
from services.assessment_settings import settings
from test_scalable_assessments import BLOB, SUB, TENANT, make_report, write_focus
from jobs.assessment_bootstrap import RUNTIME_GRANTS
from reports.builder import build_full_report
from anomalies.models import DailyCostRecord
from services.focus_history_reader import FocusHistoryData
from services.history_records import HistoryRecords, HistorySegment


def peak_process_memory() -> int:
    if sys.platform != "win32":
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    import ctypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            )
        ]
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    process = ctypes.WinDLL("psapi", use_last_error=True)
    process.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    process.GetProcessMemoryInfo.restype = ctypes.c_int
    if not process.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


@pytest.mark.enable_socket
def test_real_postgres_queue_fencing_pagination_and_concurrent_summary_reads(monkeypatch):
    dsn = os.environ.get("ASSESSMENT_TEST_DSN")
    if not dsn:
        pytest.skip("Set ASSESSMENT_TEST_DSN to an isolated loopback meghkosha_test database.")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        pytest.fail("Integration tests may only use the dedicated local meghkosha_test database.")
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    settings.cache_clear()

    async def exercise():
        db = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        monkeypatch.setattr(assessment_store, "_pool", db)
        try:
            await db.execute((Path(__file__).parent.parent / "services" / "assessment_schema.sql").read_text())
            await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
            quota_key = f"synthetic-quota-{uuid4()}"
            delays = await asyncio.gather(*(assessment_store.azure_request_delay(quota_key, 5) for _ in range(10)))
            assert sum(value == 0 for value in delays) == 1
            await assessment_store.defer_azure_requests(quota_key, 2)
            assert await assessment_store.azure_request_delay(quota_key, 0.5) > 1
            jobs = await asyncio.gather(*(assessment_store.enqueue(TENANT, "actor", [SUB], 90) for _ in range(20)))
            assert len({job["jobId"] for job in jobs}) == 1
            claims = await asyncio.gather(*(assessment_store.claim() for _ in range(10)))
            claimed = [job for job in claims if job is not None]
            assert len(claimed) == 1
            job = claimed[0]
            assert job["attempts"] == 1
            assert not await assessment_store.heartbeat(job["job_id"], uuid4())
            assert await assessment_store.heartbeat(job["job_id"], job["lease_token"])
            assert not await assessment_store.cancel(TENANT, job["job_id"], "different-actor")
            assert await assessment_store.get_job("other-tenant", job["job_id"]) is None

            snapshot = report_snapshots.build_snapshot(make_report(15000), [SUB], 90)
            await assessment_store.publish(snapshot, job, report_snapshots.snapshot_blob_name(snapshot))
            stored = await assessment_store.get_job(TENANT, job["job_id"])
            assert stored["status"] == "succeeded"
            view = await assessment_store.snapshot_view(TENANT, snapshot_id=snapshot.snapshot_id)
            assert view["report"]["executiveSummary"]["currentMonthlySpend"] == 15000
            assert view["report"]["costHierarchy"] == []
            assert {"path": "costHierarchy", "total": 15000} in view["collections"]
            assert await assessment_store.snapshot_view("other-tenant", snapshot_id=snapshot.snapshot_id) is None
            seen = set()
            cursor = -1
            while True:
                page = await assessment_store.page(TENANT, snapshot.snapshot_id, "costHierarchy", cursor, 200, None, "")
                assert len(page["items"]) <= 200
                assert len(json.dumps(page).encode()) <= settings().response_bytes
                for item in page["items"]:
                    assert item["resourceId"] not in seen
                    seen.add(item["resourceId"])
                if page["nextCursor"] is None:
                    break
                assert page["nextCursor"] > cursor
                cursor = page["nextCursor"]
            assert len(seen) == 15000
            filtered = await assessment_store.page(TENANT, snapshot.snapshot_id, "costHierarchy", -1, 200, SUB, "vm-14999")
            assert len(filtered["items"]) == 1
            assert filtered["nextCursor"] is None
            assert (await assessment_store.snapshot_catalog(TENANT, [SUB], 50))[0]["snapshotId"] == snapshot.snapshot_id
            assert (await assessment_store.snapshot_view(
                TENANT, scope=snapshot.scope_hash, before_period="2026-08-01",
            ))["snapshotId"] == snapshot.snapshot_id
            assert await assessment_store.snapshot_view(
                TENANT, scope=snapshot.scope_hash, before_period="2026-07-01",
            ) is None
            await db.execute("ANALYZE assessments.records")
            plan = json.loads(await db.fetchval(
                "EXPLAIN (FORMAT JSON) SELECT ordinal FROM assessments.records WHERE search_text ILIKE $1",
                "%vm-14999%",
            ))
            assert "records_search" in json.dumps(plan)

            latencies = []
            tracemalloc.start()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                async def read():
                    started = time.perf_counter()
                    response = await client.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")
                    latencies.append(time.perf_counter() - started)
                    assert response.status_code == 200
                    assert len(response.content) < settings().response_bytes
                    assert response.json()["report"]["executiveSummary"]["currentMonthlySpend"] == 15000
                for _ in range(3):
                    await asyncio.gather(*(read() for _ in range(10)))
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            p95 = sorted(latencies)[int(len(latencies) * 0.95)]
            assert p95 <= 2.0
            assert peak < 64 * 1024**2
            print(json.dumps({"resources": 15000, "concurrentReaders": 10, "summaryP95Seconds": p95,
                              "summaryReadPeakPythonBytes": peak, "summaryBytes": len(json.dumps(view).encode())}))

            for role in ("meghkosha_api", "meghkosha_worker"):
                if not await db.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                    await db.execute(f"CREATE ROLE {role}")
            await db.execute(RUNTIME_GRANTS.replace("DATABASE meghkosha", "DATABASE meghkosha_test"))
            async with db.acquire() as connection:
                try:
                    await connection.execute("SET ROLE meghkosha_api")
                    assert await connection.fetchval("SELECT count(*) FROM assessments.snapshots") == 1
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute("UPDATE assessments.jobs SET lease_token=NULL")
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute("CREATE TABLE assessments.must_not_exist (value int)")
                    await connection.execute("SET ROLE meghkosha_worker")
                    await connection.execute("UPDATE assessments.jobs SET lease_token=lease_token WHERE false")
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute("CREATE TABLE assessments.must_not_exist (value int)")
                finally:
                    await connection.execute("RESET ROLE")

            queued = await assessment_store.enqueue(TENANT, "actor", [SUB], 30)
            expired = await assessment_store.claim()
            rejected = report_snapshots.build_snapshot(make_report(stale_days=30), [SUB], 30)
            await db.execute(
                "CREATE OR REPLACE FUNCTION assessments.test_lease_delay() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.1); RETURN NEW; END $$;"
                "CREATE TRIGGER test_lease_delay BEFORE INSERT ON assessments.snapshots "
                "FOR EACH ROW EXECUTE FUNCTION assessments.test_lease_delay()"
            )
            try:
                await db.execute(
                    "UPDATE assessments.jobs SET lease_until=clock_timestamp()+interval '50 milliseconds' WHERE job_id=$1",
                    expired["job_id"],
                )
                with pytest.raises(assessment_store.LeaseLostError):
                    await assessment_store.publish(rejected, expired, report_snapshots.snapshot_blob_name(rejected))
            finally:
                await db.execute(
                    "DROP TRIGGER test_lease_delay ON assessments.snapshots;"
                    "DROP FUNCTION assessments.test_lease_delay()"
                )
            assert await db.fetchval("SELECT count(*) FROM assessments.snapshots WHERE snapshot_id=$1", rejected.snapshot_id) == 0
            reclaimed = await assessment_store.claim()
            assert reclaimed["attempts"] == 2
            assert reclaimed["lease_token"] != expired["lease_token"]
            await assessment_store.fail(reclaimed["job_id"], reclaimed["lease_token"], "transient", retryable=True, code="test_retry")
            await db.execute("UPDATE assessments.jobs SET next_attempt_at=now() WHERE job_id=$1", reclaimed["job_id"])
            final = await assessment_store.claim()
            assert final["attempts"] == 3
            await db.execute("UPDATE assessments.jobs SET lease_until=now()-interval '1 second' WHERE job_id=$1", final["job_id"])
            assert await assessment_store.claim() is None
            assert (await assessment_store.get_job(TENANT, UUID(queued["jobId"])))["errorCode"] == "worker_attempts_exhausted"

            monkeypatch.setenv("ASSESSMENT_QUEUE_CAPACITY", "1")
            settings.cache_clear()
            await assessment_store.enqueue(TENANT, "actor", [SUB], 7)
            with pytest.raises(assessment_store.QueueFullError):
                await assessment_store.enqueue(TENANT, "actor", [SUB], 14)
            assert (await assessment_store.snapshot_view(TENANT, snapshot_id=snapshot.snapshot_id)) is not None
        finally:
            await assessment_store.close()
            settings.cache_clear()
    asyncio.run(exercise())


@pytest.mark.enable_socket
def test_one_million_focus_rows_are_streamed_with_bounded_process_memory(tmp_path, monkeypatch):
    if os.environ.get("ASSESSMENT_RUN_SCALE_TESTS") != "1":
        pytest.skip("Set ASSESSMENT_RUN_SCALE_TESTS=1 for the million-row synthetic ingestion gate.")
    path = tmp_path / "million.csv.gz"
    try:
        with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=1) as stream:
            write_focus(stream, rows=1_000_000, resources=15_000)
        settings.cache_clear()
        token = active_budget.set(AssessmentBudget(settings(), tmp_path))
        started = time.perf_counter()
        try:
            parsed = focus_cost_reader.build_focus_cost_data({BLOB: path}, [SUB])
            peak = peak_process_memory()
        finally:
            active_budget.reset(token)
        assert parsed.row_count == 1_000_000
        assert len(parsed.resource_costs) == 15_000
        assert parsed.effective_cost_by_subscription[SUB] == 10_000
        assert peak < 512 * 1024**2
        print(json.dumps({"focusRows": parsed.row_count, "resources": len(parsed.resource_costs),
                          "parseSeconds": time.perf_counter() - started, "peakProcessWorkingSetBytes": peak}))
        dsn = os.environ.get("ASSESSMENT_TEST_DSN")
        if dsn:
            target = urlsplit(dsn)
            if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
                pytest.fail("Scale publication is restricted to the isolated local meghkosha_test database.")
            monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
            base = make_report()
            report = build_full_report(
                subscription_ids=[SUB], subscription_names=parsed.subscription_names,
                per_sub_spend=parsed.effective_cost_by_subscription, resource_graph_rows={},
                cost_by_resource_id=parsed.effective_cost_by_resource_id,
                service_family_spend=parsed.service_family_spend, advisor_recommendations=[], untagged_counts={},
                service_spend=parsed.provider_spend, report_metadata=base.report_metadata,
                completeness=base.completeness, focus_cost_data=parsed,
                include_all_resource_costs=True,
            )
            assert len(report.cost_hierarchy) == 15000
            assert report.executive_summary.current_monthly_spend == 10000

            async def publish_and_read():
                db = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
                monkeypatch.setattr(assessment_store, "_pool", db)
                try:
                    await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
                    await assessment_store.enqueue(TENANT, "synthetic-scale", [SUB], 90)
                    job = await assessment_store.claim()
                    snapshot = report_snapshots.build_snapshot(report, [SUB], 90)
                    await assessment_store.publish(snapshot, job, report_snapshots.snapshot_blob_name(snapshot))
                    timings = []
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                        async def read():
                            began = time.perf_counter()
                            response = await client.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")
                            timings.append(time.perf_counter() - began)
                            assert response.status_code == 200
                            assert len(response.content) < settings().response_bytes
                            value = response.json()
                            assert value["report"]["executiveSummary"]["currentMonthlySpend"] == 10000
                            assert {"path": "costHierarchy", "total": 15000} in value["collections"]
                        for _ in range(3):
                            await asyncio.gather(*(read() for _ in range(10)))
                    p95 = sorted(timings)[int(len(timings) * 0.95)]
                    assert p95 <= 2.0
                    working_set = peak_process_memory()
                    assert working_set < 2 * 1024**3
                    print(json.dumps({"combinedFocusRows": 1_000_000, "publishedResources": 15000,
                                      "concurrentReaders": 10, "summaryP95Seconds": p95,
                                      "pipelinePeakWorkingSetBytes": working_set}))
                finally:
                    await assessment_store.close()
            asyncio.run(publish_and_read())
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.enable_socket
def test_reserved_read_pool_survives_slow_work_connections(monkeypatch):
    dsn = os.environ.get("ASSESSMENT_TEST_DSN")
    if not dsn:
        pytest.skip("Set the isolated local PostgreSQL DSN.")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        pytest.fail("Only the dedicated loopback test database is supported.")
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("ASSESSMENT_DB_USER", "meghkosha_api")

    async def exercise():
        work_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
        read_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
        monkeypatch.setattr(assessment_store, "_pool", work_pool)
        monkeypatch.setattr(assessment_store, "_read_pool", read_pool)
        await work_pool.execute((Path(__file__).parent.parent / "services" / "assessment_schema.sql").read_text())
        await work_pool.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
        await assessment_store.enqueue(TENANT, "read-reservation-seed", [SUB], 90)
        seed_job = await assessment_store.claim()
        seed = report_snapshots.build_snapshot(make_report(15000), [SUB], 90)
        await assessment_store.publish(seed, seed_job, report_snapshots.snapshot_blob_name(seed))
        occupied = asyncio.Event()

        async def hold_work():
            async with work_pool.acquire() as connection:
                occupied.set()
                await connection.execute("SELECT pg_sleep(2)")

        held = asyncio.create_task(hold_work())
        try:
            await occupied.wait()
            started = time.perf_counter()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                responses = await asyncio.gather(*(
                    client.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90") for _ in range(10)
                ))
            assert all(response.status_code == 200 for response in responses)
            assert time.perf_counter() - started < 2
            assert not held.done()
        finally:
            await held
            await assessment_store.close()
    asyncio.run(exercise())


@pytest.mark.enable_socket
def test_retained_million_row_history_is_replayable_and_publishes_complete_daily_details(tmp_path, monkeypatch):
    if os.environ.get("ASSESSMENT_RUN_SCALE_TESTS") != "1":
        pytest.skip("Enable the retained-history scale gate explicitly.")
    settings.cache_clear()
    budget = AssessmentBudget(settings(), tmp_path)
    token = active_budget.set(budget)
    try:
        segments = []
        for period, days in (("2026-06", 30), ("2026-07", 31)):
            segment = HistorySegment(budget)
            for index in range(500000):
                resource = index % 15000
                segment.append(DailyCostRecord(
                    date=f"{period}-{index // 15000 % days + 1:02d}",
                    subscription_id=SUB, subscription_name="Synthetic",
                    service_name="Virtual Machines", resource_group="rg",
                    resource_id=f"/subscriptions/{SUB}/resources/vm-{resource}",
                    resource_name=f"vm-{resource}", effective_cost=0.01,
                    service_category="Compute", resource_type="Microsoft.Compute/virtualMachines",
                    region="eastus", charge_category="Usage", list_cost=0.01,
                    contracted_cost=0.01, tags={"team": "synthetic"},
                ))
            segment.flush()
            segments.append(segment)
        records = HistoryRecords(segments)
        assert len(records) == 1000000
        history = FocusHistoryData(
            currency="USD", history_start="2026-06-01", history_end="2026-07-31",
            complete_days=61, periods=["2026-06", "2026-07"], subscription_ids=[SUB],
            subscription_names={SUB: "Synthetic"}, records=records,
        )
        base = make_report()
        report = build_full_report(
            subscription_ids=[SUB], subscription_names={SUB: "Synthetic"},
            per_sub_spend={SUB: 5000.0}, resource_graph_rows={}, cost_by_resource_id={},
            service_family_spend={}, advisor_recommendations=[], untagged_counts={},
            report_metadata=base.report_metadata, completeness=base.completeness,
            focus_history=history, include_all_resource_costs=True,
            detail_max_rows=settings().detail_rows, detail_max_points=settings().detail_points,
        )
        assert report.cost_details.status == "complete"
        assert len(report.cost_details.rows) == 15000
        assert sum(len(row.daily_costs) for row in report.cost_details.rows) == 915000
        assert round(sum(sum(row.daily_costs.values()) for row in report.cost_details.rows), 2) == 10000
        assert peak_process_memory() < 2 * 1024**3
        print(json.dumps({
            "retainedHistoryRows": len(records), "days": 61, "resourceDetailRows": 15000,
            "dailyPoints": 915000, "peakWorkingSetBytes": peak_process_memory(),
            "spooledBytes": budget.used["spool_bytes"],
        }))
        dsn = os.environ.get("ASSESSMENT_TEST_DSN")
        if dsn:
            target = urlsplit(dsn)
            if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
                pytest.fail("Only the dedicated loopback test database is supported.")
            monkeypatch.setenv("ASSESSMENT_MODE", "postgres")

            async def publish_during_reads():
                db = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
                monkeypatch.setattr(assessment_store, "_pool", db)
                publishing = None
                try:
                    await db.execute((Path(__file__).parent.parent / "services" / "assessment_schema.sql").read_text())
                    await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
                    await assessment_store.enqueue(TENANT, "retained-history-seed", [SUB], 90)
                    seed_job = await assessment_store.claim()
                    seed = report_snapshots.build_snapshot(make_report(10000), [SUB], 90)
                    await assessment_store.publish(seed, seed_job, report_snapshots.snapshot_blob_name(seed))
                    await assessment_store.enqueue(TENANT, "retained-history-scale", [SUB], 90)
                    job = await assessment_store.claim()
                    snapshot = report_snapshots.build_snapshot(report, [SUB], 90)
                    publishing = asyncio.create_task(assessment_store.publish(
                        snapshot, job, report_snapshots.snapshot_blob_name(snapshot),
                    ))
                    timings = []
                    rounds = 0
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                        async def read():
                            began = time.perf_counter()
                            response = await client.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")
                            timings.append(time.perf_counter() - began)
                            assert response.status_code == 200
                            value = response.json()
                            assert value["report"]["executiveSummary"]["currentMonthlySpend"] in {5000, 10000}
                            if value["snapshotId"] == snapshot.snapshot_id:
                                assert value["report"]["costDetails"]["status"] == "complete"
                        while not publishing.done() or rounds < 3:
                            await asyncio.gather(*(read() for _ in range(10)))
                            rounds += 1
                            await asyncio.sleep(0.02)
                    await publishing
                    details = await assessment_store.page(
                        TENANT, snapshot.snapshot_id, "costDetails.rows", -1, 100, SUB, "vm-14999",
                    )
                    assert len(details["items"]) == 1
                    assert len(details["items"][0]["dailyCosts"]) == 61
                    p95 = sorted(timings)[min(int(len(timings) * 0.95), len(timings) - 1)]
                    assert p95 < 2
                    assert peak_process_memory() < 2 * 1024**3
                    print(json.dumps({
                        "retainedPublicationReadP95Seconds": p95,
                        "readRequestsDuringPublication": len(timings), "concurrentReaders": 10,
                        "pipelinePeakWorkingSetBytes": peak_process_memory(),
                    }))
                finally:
                    if publishing is not None and not publishing.done():
                        publishing.cancel()
                        try:
                            await publishing
                        except asyncio.CancelledError:
                            pass
                    await assessment_store.close()
            asyncio.run(publish_during_reads())
    finally:
        active_budget.reset(token)
