import asyncio
import json
import os
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from services.assessment_queries import CostQuery, execute
from services.assessment_settings import settings


@pytest.mark.enable_socket
def test_40000_resource_six_calendar_month_sql_serving():
    if os.environ.get("RUN_MANUAL_SCALE_TEST") != "1":
        pytest.skip("Set RUN_MANUAL_SCALE_TEST=1 for the explicit 7.24-million-point local SQL check.")
    dsn = os.environ.get("ASSESSMENT_TEST_DSN", "")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        pytest.fail("Scale checks require the dedicated local meghkosha_test database.")

    async def exercise():
        db = await asyncpg.connect(dsn, command_timeout=300)
        tenant, snapshot = str(uuid4()), uuid4().hex[:24]
        subscription = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        dates = [str(date(2026, 1, 1) + timedelta(days=index)) for index in range(181)]
        try:
            await db.execute((Path(__file__).parents[1] / "services" / "assessment_schema.sql").read_text())
            await db.execute("SET statement_timeout='5min'")
            await db.execute("TRUNCATE assessments.snapshots CASCADE")
            await db.execute(
                "INSERT INTO assessments.snapshots "
                "(tenant_id,snapshot_id,scope_hash,subscription_ids,stale_days,created_at,summary,blob_name,published) "
                "VALUES($1,$2,'synthetic-scale',$3::jsonb,90,now(),'{}','synthetic-scale',true)",
                tenant, snapshot, json.dumps([subscription]),
            )
            started = time.perf_counter()
            await db.execute(
                "INSERT INTO assessments.cost_rows "
                "SELECT $1,$2,'detail-'||n,n,$3,"
                "'/subscriptions/'||$3||'/resourceGroups/synthetic/providers/Microsoft.Compute/virtualMachines/vm-'||n,"
                "'resource-'||n,'synthetic','Virtual Machines','centralindia','Microsoft.Compute/virtualMachines',"
                "'{}'::jsonb,jsonb_build_object('detailId','detail-'||n,'subscriptionId',$3::text,"
                "'subscriptionName','Synthetic scope','resourceName','vm-'||n,"
                "'resourceId','/subscriptions/'||$3||'/resourceGroups/synthetic/providers/Microsoft.Compute/virtualMachines/vm-'||n,"
                "'resourceType','Microsoft.Compute/virtualMachines','resourceGroup','synthetic',"
                "'serviceName','Virtual Machines','region','centralindia','tags','{}'::jsonb,"
                "'tagAttributionSource','exported_resource_tags') FROM generate_series(1,40000) n",
                tenant, snapshot, subscription,
            )
            await db.execute("ANALYZE assessments.cost_rows")
            async with db.transaction():
                points = []
                async for row in db.cursor(
                    "SELECT r.detail_id,date '2026-01-01'+day AS charge_date "
                    "FROM generate_series(0,180) day CROSS JOIN assessments.cost_rows r "
                    "WHERE r.tenant_id=$1 AND r.snapshot_id=$2", tenant, snapshot, prefetch=5000, timeout=300,
                ):
                    points.append((tenant, snapshot, row["detail_id"], row["charge_date"], 1.0))
                    if len(points) == 5000:
                        await db.copy_records_to_table(
                            "cost_daily", schema_name="assessments", records=points,
                            columns=("tenant_id", "snapshot_id", "detail_id", "charge_date", "amount"), timeout=300,
                        )
                        points.clear()
                if points:
                    await db.copy_records_to_table(
                        "cost_daily", schema_name="assessments", records=points,
                        columns=("tenant_id", "snapshot_id", "detail_id", "charge_date", "amount"), timeout=300,
                    )
            await db.execute("ANALYZE assessments.cost_daily")
            load_seconds = time.perf_counter() - started
            print(json.dumps({"phase": "seeded", "dailyPoints": 7240000,
                              "sqlSeedSeconds": round(load_seconds, 3)}), flush=True)
            query = {"window": {"startDate": dates[0], "endDate": dates[-1]}}
            tracemalloc.start()
            started = time.perf_counter()
            metrics = await execute(db, tenant, snapshot, CostQuery(kind="metrics", **query), dates)
            metrics_seconds = time.perf_counter() - started
            started = time.perf_counter()
            page = await execute(db, tenant, snapshot, CostQuery(kind="groups", limit=100, **query), dates)
            page_seconds = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert metrics["currentTotal"] == 7_240_000
            assert metrics["complete"] and not metrics["previousComplete"]
            assert metrics["previousTotal"] == 0
            assert page["total"] == 40_000 and len(page["items"]) == 100
            assert page["currentTotal"] == 7_240_000 and page["nextOffset"] == 100
            assert all(item["current"] == 181 for item in page["items"])
            assert all(item["previous"] is None for item in page["items"])
            encoded = len(json.dumps(page).encode())
            assert encoded < settings().response_bytes
            assert peak < 64 * 1024**2
            print(json.dumps({
                "resources": 40000, "calendarDays": 181, "dailyPoints": 7240000,
                "sqlSeedSeconds": round(load_seconds, 3), "metricsSeconds": round(metrics_seconds, 3),
                "resourcePageSeconds": round(page_seconds, 3), "pageBytes": encoded,
                "clientPeakPythonBytes": peak,
                "scope": "Synthetic normalized PostgreSQL serving; not a full-volume Azure upload/enrichment certification",
            }))
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
            await db.execute("DELETE FROM assessments.snapshots WHERE tenant_id=$1 AND snapshot_id=$2", tenant, snapshot)
            await db.close()

    asyncio.run(exercise())
