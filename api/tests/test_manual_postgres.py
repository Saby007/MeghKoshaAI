import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
import pytest

import main
from jobs import assessment_task
from services import assessment_store, focus_uploads, report_snapshots, target_connection
from services.assessment_settings import settings
from test_manual_focus import SUB, OTHER, write_report


class MemoryStorage:
    def __init__(self):
        self.content = {}
        self.blocks = {}

    def get_blob_client(self, name):
        storage = self

        class Blob:
            async def stage_block(self, identifier, data, **kwargs):
                storage.blocks[name, identifier] = bytes(data)

            async def commit_block_list(self, identifiers):
                storage.content[name] = b"".join(storage.blocks[name, identifier] for identifier in identifiers)

            async def download_blob(self, **kwargs):
                class Downloader:
                    async def chunks(self):
                        yield storage.content[name]
                return Downloader()
        return Blob()


@pytest.mark.enable_socket
def test_real_database_upload_only_replacement_resume_and_cross_workspace_isolation(monkeypatch, tmp_path):
    dsn = os.environ.get("ASSESSMENT_TEST_DSN")
    if not dsn:
        pytest.skip("Set ASSESSMENT_TEST_DSN to an isolated loopback meghkosha_test database.")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        pytest.fail("Only a dedicated local meghkosha_test database is allowed.")
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("ASSESSMENT_WORKER_PROCESS", "true")
    settings.cache_clear()
    storage = MemoryStorage()

    @asynccontextmanager
    async def container():
        yield storage

    async def forbidden_token():
        pytest.fail("A no-SP upload attempted a target Azure authentication call.")

    async def no_archive(snapshot, **kwargs):
        return None

    monkeypatch.setattr(focus_uploads, "container", container)
    monkeypatch.setattr(target_connection, "access_token", forbidden_token)
    monkeypatch.setattr(report_snapshots, "publish_snapshot", no_archive)

    async def exercise():
        db = await asyncpg.create_pool(dsn, min_size=1, max_size=8)

        async def pool(**kwargs):
            return db

        async def close():
            return None

        monkeypatch.setattr(assessment_store, "pool", pool)
        monkeypatch.setattr(assessment_store, "close", close)
        try:
            await db.execute((Path(__file__).parent.parent / "services" / "assessment_schema.sql").read_text())
            await db.execute("TRUNCATE assessments.workspaces,assessments.jobs,assessments.snapshots CASCADE")
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://manual.test") as first, \
                    httpx.AsyncClient(transport=transport, base_url="https://manual.test") as second:
                session = (await first.get("/api/session")).json()
                other_session = (await second.get("/api/session")).json()
                assert session["userId"] != other_session["userId"]
                headers = {"X-CSRF-Token": session["csrfToken"]}
                assert (await first.put("/api/admin/configuration", json={"subscriptionId": SUB})).status_code == 403
                response = await first.put("/api/admin/configuration", json={"subscriptionId": SUB}, headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["connection"]["hasCredentials"] is False
                assert (await second.get("/api/admin/configuration")).json() == {"connection": None}
                secret = "synthetic-secret-must-not-be-echoed"
                invalid = await first.put("/api/admin/configuration", headers=headers,
                                          json={"subscriptionId": SUB, "clientSecret": secret})
                assert invalid.status_code == 422 and secret not in invalid.text

                async def upload(rows):
                    source = write_report(tmp_path / "input.csv", rows).read_bytes()
                    response = await first.post("/api/admin/uploads", headers=headers,
                                                json={"files": [{"name": "report.csv", "size": len(source)}]})
                    assert response.status_code == 201, response.text
                    report = response.json()
                    endpoint = f"/api/admin/uploads/{report['datasetId']}/files/{report['files'][0]['fileId']}"
                    response = await first.put(f"{endpoint}?offset=0", content=source, headers=headers)
                    assert response.status_code == 200, response.text
                    assert (await first.put(f"{endpoint}?offset=0", content=source, headers=headers)).status_code == 200
                    assert (await first.put(f"{endpoint}?offset=0", content=b"x" * len(source), headers=headers)).status_code == 409
                    checkpoints = (await first.get(f"{endpoint}/chunks")).json()
                    assert len(checkpoints["items"]) == 1
                    assert checkpoints["items"][0]["length"] == len(source)
                    response = await first.post(f"/api/admin/uploads/{report['datasetId']}/complete", headers=headers)
                    assert response.status_code == 202, response.text
                    again = await first.post(f"/api/admin/uploads/{report['datasetId']}/complete", headers=headers)
                    assert again.json()["jobId"] == response.json()["jobId"]
                    return report, response.json()

                async def process():
                    job = await assessment_store.claim()
                    assert job is not None
                    directory = tmp_path / str(job["job_id"])
                    directory.mkdir()
                    monkeypatch.setenv("ASSESSMENT_WORKSPACE", str(directory))
                    await assessment_task.run(job["job_id"], job["lease_token"])
                    return await assessment_store.get_job(session["userId"], job["job_id"])

                first_upload, first_job = await upload(
                    [("2026-01-01", "10.25", {}), ("2026-01-02", "-2.25", {})]
                    + [("2026-01-01", "0", {})] * 1000,
                )
                assert first_upload["files"][0]["size"] > 65536
                assert (await second.get(f"/api/admin/uploads/{first_upload['datasetId']}")).status_code == 404
                assert (await second.get(f"/api/assessments/jobs/{first_job['jobId']}")).status_code == 404
                job = await process()
                assert job["status"] == "succeeded", job
                response = await first.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")
                assert response.status_code == 200, response.text
                original = response.json()
                assert original["report"]["executiveSummary"]["currentMonthlySpend"] == 8
                assert original["report"]["reportMetadata"]["enrichmentStatus"] == "not_configured"
                costs = await first.post(
                    f"/api/report/{original['snapshotId']}/cost-query", headers=headers,
                    json={"kind": "metrics", "window": {"startDate": "2026-01-01", "endDate": "2026-01-02"}},
                )
                assert costs.status_code == 200, costs.text
                assert costs.json()["currentTotal"] == 8
                assert costs.json()["complete"] and not costs.json()["previousComplete"]
                assert await db.fetchval(
                    "SELECT sum(amount) FROM assessments.cost_daily WHERE tenant_id=$1 AND snapshot_id=$2",
                    session["userId"], original["snapshotId"],
                ) == 8
                assert (await second.get(f"/api/assessments/snapshots/{original['snapshotId']}")).status_code == 404
                second_upload, _ = await upload([("2026-01-01", "3", {})])
                assert (await first.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")).json()["snapshotId"] == original["snapshotId"]
                assert (await process())["status"] == "succeeded"
                replacement = (await first.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")).json()
                assert replacement["report"]["executiveSummary"]["currentMonthlySpend"] == 3
                assert replacement["snapshotId"] != original["snapshotId"]
                assert str(await db.fetchval("SELECT active_dataset FROM assessments.workspaces WHERE workspace_id=$1",
                                            UUID(session["userId"]))) == second_upload["datasetId"]
                await upload([("2026-01-01", "999", {"SubAccountId": OTHER})])
                failed = await process()
                assert failed["status"] == "failed" and failed["errorCode"] == "invalid_focus_report"
                unchanged = (await first.get(f"/api/report/latest?subscriptionId={SUB}&staleDays=90")).json()
                assert unchanged["snapshotId"] == replacement["snapshotId"]
                for path, method in [("/api/budgets", "GET"), ("/api/budgets", "POST"),
                                     ("/api/schedules", "POST"), ("/api/focus-exports", "GET")]:
                    result = await first.request(method, path, headers=headers)
                    assert result.status_code in {404, 405}, (path, result.text)
        finally:
            await db.close()
            settings.cache_clear()

    asyncio.run(exercise())
