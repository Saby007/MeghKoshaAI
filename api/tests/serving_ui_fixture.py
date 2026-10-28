"""Loopback-only synthetic SQL fixture server for original-dashboard browser tests."""

import argparse
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import uvicorn
from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from anomalies.models import AnomalySummary
from reports.models import FullReport
from services import assessment_queries, assessment_records, assessment_store, report_snapshots
from services.assessment_settings import settings

TENANT = "11111111-1111-1111-1111-111111111111"
fixture: dict = {}


@asynccontextmanager
async def lifespan(app):
    dsn = os.environ["ASSESSMENT_TEST_DSN"]
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        raise RuntimeError("UI fixtures require the dedicated loopback synthetic database.")
    source = (ROOT.parent / "web" / "src" / "report" / "testFixtures.ts").as_uri()
    script = f"""
        import * as f from {json.dumps(source)};
        const report = {{...f.resourceReportFixture, costDetails: f.detailReportFixture.costDetails}};
        report.costHierarchy = report.costDetails.rows.map(row => ({{
            subscriptionId: row.subscriptionId, subscriptionName: row.subscriptionName,
            resourceGroup: row.resourceGroup, resourceId: row.resourceId, resourceName: row.resourceName,
            resourceType: row.resourceType, monthlySpend: Object.entries(row.dailyCosts)
                .filter(([day]) => day.startsWith('2026-08')).reduce((sum, [,cost]) => sum + cost, 0), pctOfTotal: 0.5,
        }}));
        report.tagCosts = {{
            available: true, status: 'Synthetic application costs', totalSpend: 3120, growthRate: null,
            dimensions: [{{tagKey: 'application', unallocatedCost: 0, rows: [
                {{value: 'Finance', monthlyCost: 2340, pctOfTotal: 0.75, forecastNextMonth: null}},
                {{value: 'Platform', monthlyCost: 780, pctOfTotal: 0.25, forecastNextMonth: null}},
            ]}}],
        }};
        process.stdout.write(JSON.stringify({{report, anomaly: f.anomalyFixture, rates: f.rateOptimizationFixture}})
            .replaceAll('sub-1', {json.dumps(TENANT)}));
    """
    payload = json.loads(subprocess.check_output(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script], text=True,
    ))
    os.environ["ASSESSMENT_MODE"] = "postgres"
    os.environ["AZURE_TENANT_ID"] = TENANT
    settings.cache_clear()
    db = await asyncpg.create_pool(dsn, min_size=1, max_size=5)

    async def get_pool(**kwargs):
        return db

    assessment_store.pool = get_pool
    try:
        await db.execute((ROOT / "services" / "assessment_schema.sql").read_text())
        await db.execute("TRUNCATE assessments.jobs,assessments.snapshots CASCADE")
        report = FullReport.model_validate(payload["report"])
        report._anomaly_evidence = AnomalySummary.model_validate(payload["anomaly"])
        snapshot = report_snapshots.build_snapshot(report, [TENANT], 90)
        await assessment_store.enqueue(TENANT, "synthetic-browser-actor", [TENANT], 90)
        await assessment_store.publish(snapshot, await assessment_store.claim(), "synthetic-archive")
        previous_worker = os.environ.get("ASSESSMENT_WORKER_PROCESS")
        try:
            os.environ["ASSESSMENT_WORKER_PROCESS"] = "true"
            reconstructed = await assessment_store.sql_snapshot(TENANT, snapshot.snapshot_id, complete=True)
            if reconstructed.report.model_dump() != report.model_dump():
                raise RuntimeError("SQL worker reconstruction changed the original fixture report.")
        finally:
            if previous_worker is None:
                os.environ.pop("ASSESSMENT_WORKER_PROCESS", None)
            else:
                os.environ["ASSESSMENT_WORKER_PROCESS"] = previous_worker
        fixture.update({
            "full": snapshot.model_dump(by_alias=True, mode="json"),
            "sql": await assessment_store.snapshot_view(TENANT, snapshot_id=snapshot.snapshot_id),
            "anomaly": payload["anomaly"], "rates": payload["rates"], "db": db,
        })
        yield
    finally:
        await db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/fixture")
async def data():
    return {key: value for key, value in fixture.items() if key != "db"}


def published(snapshot_id: str):
    if snapshot_id != fixture["sql"]["snapshotId"]:
        raise HTTPException(404, "Only the dedicated synthetic fixture is available.")
    return fixture["sql"]


@app.post("/api/report/{snapshot_id}/cost-query")
async def costs(snapshot_id: str, query: assessment_queries.CostQuery):
    metadata = published(snapshot_id)
    async with fixture["db"].acquire() as db:
        return await assessment_queries.execute(
            db, TENANT, snapshot_id, query, metadata["report"]["costDetails"]["dates"],
        )


@app.post("/api/report/{snapshot_id}/records-query")
async def records(snapshot_id: str, query: assessment_records.RecordQuery):
    metadata = published(snapshot_id)
    async with fixture["db"].acquire() as db:
        return await assessment_records.project(db, TENANT, snapshot_id, query, metadata["collections"])


@app.post("/api/report/{snapshot_id}/hierarchy-query")
async def hierarchy(snapshot_id: str, query: assessment_records.HierarchyQuery):
    published(snapshot_id)
    async with fixture["db"].acquire() as db:
        return await assessment_records.hierarchy(db, TENANT, snapshot_id, query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning", http="h11", ws="none")
