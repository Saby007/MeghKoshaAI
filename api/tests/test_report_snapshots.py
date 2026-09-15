import asyncio
import json

import pytest
from azure.core.exceptions import ResourceNotFoundError

from reports.builder import build_full_report
from services import report_snapshots


class FakeDownloader:
    def __init__(self, content: bytes):
        self.content = content

    def readall(self):
        return self.content


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bool]] = []

    def upload_blob(self, name, data, overwrite, content_settings):
        if name in self.blobs and not overwrite:
            from azure.core.exceptions import ResourceExistsError

            raise ResourceExistsError("exists")
        self.blobs[name] = data
        self.uploads.append((name, overwrite))

    def download_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return FakeDownloader(self.blobs[name])

    def list_blobs(self, name_starts_with):
        from types import SimpleNamespace

        return [SimpleNamespace(name=name) for name in self.blobs if name.startswith(name_starts_with)]


def _report(complete=True):
    report = build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
    )
    return report.model_copy(update={
        "completeness": report.completeness.model_copy(update={"complete": complete}),
        "report_metadata": report.report_metadata.model_copy(update={"period": "2026-07"}),
    })


def test_publishes_immutable_payload_before_scope_and_global_pointers(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(report_snapshots, "get_container_client", lambda: container)
    snapshot = report_snapshots.build_snapshot(
        _report(), ["SUB-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )

    asyncio.run(report_snapshots.publish_snapshot(snapshot))

    assert container.uploads[0] == (
        f"scopes/{snapshot.scope_hash}/snapshots/2026-07/{snapshot.snapshot_id}.json",
        False,
    )
    assert container.uploads[-3:] == [
        (f"scopes/{snapshot.scope_hash}/snapshots/2026-07/{snapshot.snapshot_id}.json.manifest.json", False),
        (f"scopes/{snapshot.scope_hash}/latest.json", True),
        ("latest.json", True),
    ]
    loaded = asyncio.run(report_snapshots.load_latest_snapshot(["sub-1"], 90))
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.report.executive_summary.current_monthly_spend == 100
    catalog = asyncio.run(report_snapshots.list_snapshots(["sub-1"]))
    assert [item.snapshot_id for item in catalog] == [snapshot.snapshot_id]
    historical = asyncio.run(report_snapshots.load_snapshot(snapshot.snapshot_id))
    assert historical.snapshot_id == snapshot.snapshot_id


@pytest.mark.parametrize("legacy", [False, True])
def test_sql_classification_survives_snapshot_storage_and_old_payloads_load(monkeypatch, legacy):
    from findings.engine import build_report

    container = FakeContainer()
    monkeypatch.setattr(report_snapshots, "get_container_client", lambda: container)
    findings = build_report(["sub-1"], 0, {"sql_databases_and_pools": [{
        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Sql/servers/server/databases/app",
        "name": "app",
        "type": "microsoft.sql/servers/databases",
        "elasticPoolId": "",
        "subscriptionId": "sub-1",
    }]}, {})
    report = _report()
    report.domains["sql"].categories = findings.tier_a_categories
    snapshot = report_snapshots.build_snapshot(report, ["sub-1"], 90, created_at="2026-09-07T06:00:00+00:00")
    if legacy:
        payload = snapshot.model_dump(by_alias=True)
        del payload["report"]["domains"]["sql"]["categories"][0]["lines"][0]["sqlContext"]
        snapshot = type(snapshot).model_validate(payload)
    asyncio.run(report_snapshots.publish_snapshot(snapshot))

    loaded = asyncio.run(report_snapshots.load_latest_snapshot(["sub-1"], 90))
    context = loaded.report.domains["sql"].categories[0].lines[0].sql_context
    if legacy:
        assert context is None
    else:
        assert context.deployment_model == "single_database"
        assert context.schema_version == "1.0"


def test_rejects_incomplete_or_mismatched_report_scope():
    with pytest.raises(ValueError, match="complete reports"):
        report_snapshots.build_snapshot(_report(complete=False), ["sub-1"], 90)
    with pytest.raises(ValueError, match="scope does not match"):
        report_snapshots.build_snapshot(_report(), ["sub-2"], 90)


def test_detects_tampered_snapshot_and_missing_scope(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(report_snapshots, "get_container_client", lambda: container)
    snapshot = report_snapshots.build_snapshot(
        _report(), ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )
    asyncio.run(report_snapshots.publish_snapshot(snapshot))
    pointer = json.loads(container.blobs["latest.json"])
    container.blobs[pointer["blobName"]] += b"tampered"

    with pytest.raises(report_snapshots.ReportSnapshotIntegrityError, match="byte length"):
        asyncio.run(report_snapshots.load_latest_snapshot())
    with pytest.raises(report_snapshots.ReportSnapshotNotFoundError):
        asyncio.run(report_snapshots.load_latest_snapshot(["sub-2"], 90))


def test_catalog_filters_scope_and_historical_load_checks_digest(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(report_snapshots, "get_container_client", lambda: container)
    first = report_snapshots.build_snapshot(
        _report(), ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )
    asyncio.run(report_snapshots.publish_snapshot(first))
    second = report_snapshots.build_snapshot(
        _report(), ["sub-1"], 90, created_at="2026-08-17T09:00:00+00:00"
    )
    asyncio.run(report_snapshots.publish_snapshot(second))

    catalog = asyncio.run(report_snapshots.list_snapshots(["SUB-1"], limit=1))
    assert [item.snapshot_id for item in catalog] == [second.snapshot_id]
    assert asyncio.run(report_snapshots.list_snapshots(["sub-2"])) == []
    manifest_name = next(name for name in container.blobs if name.endswith(f"/{first.snapshot_id}.json.manifest.json"))
    manifest = json.loads(container.blobs[manifest_name])
    container.blobs[manifest["blobName"]] += b"tampered"
    with pytest.raises(report_snapshots.ReportSnapshotIntegrityError, match="byte length"):
        asyncio.run(report_snapshots.load_snapshot(first.snapshot_id))