import asyncio
import json

import pytest

from services.assessment_records import HierarchyQuery, RecordQuery, hierarchy, project
from services.assessment_store import record_rows
from test_assessment_query_postgres import OTHER_TENANT, SNAPSHOT, SUB, TENANT, connect

pytestmark = pytest.mark.enable_socket


async def collection(db, tenant, path, values):
    await db.execute("INSERT INTO assessments.collections VALUES ($1,$2,$3,$4)", tenant, SNAPSHOT, path, len(values))
    await db.executemany(
        "INSERT INTO assessments.records VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)",
        list(record_rows(tenant, SNAPSHOT, path, values)),
    )


def test_real_record_projection_preserves_full_filter_totals_and_isolation():
    async def run():
        db = await connect()
        try:
            paths = ["tierACategories.0.lines", "tierACategories.1.lines"]
            contexts = {
                paths[0]: {"category": "unattached_disks", "displayName": "Unattached disks", "impactType": "potential_savings"},
                paths[1]: {"category": "expired_support", "displayName": "Expired support", "impactType": "cost_at_risk"},
            }
            await db.execute("UPDATE assessments.snapshots SET collection_context=$3::jsonb WHERE tenant_id=$1 AND snapshot_id=$2",
                             TENANT, SNAPSHOT, json.dumps(contexts))
            for index, path in enumerate(paths):
                await collection(db, TENANT, path, [{
                    "resourceId": f"resource-{index}", "resourceName": f"VM {index}",
                    "subscriptionId": SUB, "subscriptionName": "Synthetic scope",
                    "monthlyCost": 10 + index * 10, "evidenceType": "metrics",
                    "sqlContext": {"deploymentModel": "single_database", "optimizationChecks": [{"status": "review"}]},
                }])
            await collection(db, OTHER_TENANT, paths[0], [{"monthlyCost": 999999, "resourceName": "PRIVATE"}])
            manifest = [{"path": path, "total": 1} for path in paths]
            body = RecordQuery(paths=paths, limit=1, sums={
                "saving": {"field": "monthlyCost", "equals": {"@impactType": "potential_savings"}},
                "risk": {"field": "monthlyCost", "equals": {"@impactType": "cost_at_risk"}},
            }, facets={"subscriptionId": "subscriptionName"})
            result = await project(db, TENANT, SNAPSHOT, body, manifest)
            assert result["total"] == 2 and result["nextOffset"] == 1
            assert result["sums"] == {"saving": 10, "risk": 20}
            assert result["facets"]["subscriptionId"] == [[SUB, "Synthetic scope"]]
            assert result["items"][0]["context"]["category"] == "unattached_disks"
            filtered = await project(db, TENANT, SNAPSHOT, RecordQuery(
                paths=paths, search="unattached", searchFields=["@displayName"],
                hasArrays=["sqlContext.optimizationChecks"],
                arrayMatches=[{"path": "sqlContext.optimizationChecks", "field": "status", "value": "review"}],
            ), manifest)
            assert filtered["total"] == 1 and filtered["items"][0]["value"]["resourceName"] == "VM 0"
            with pytest.raises(ValueError, match="not available"):
                await project(db, TENANT, SNAPSHOT, RecordQuery(paths=["private.path"]), manifest)
        finally:
            await db.close()
    asyncio.run(run())


def test_real_aggregate_metadata_and_paged_picker_values():
    async def run():
        db = await connect()
        try:
            path = "tagCosts.dimensions.0.rows"
            await collection(db, TENANT, path, [
                {"value": "large", "monthlyCost": 9}, {"value": "small", "monthlyCost": 2},
                {"value": "credit", "monthlyCost": -1},
            ])
            manifest = [{"path": path, "total": 3}]
            metadata = await project(db, TENANT, SNAPSHOT, RecordQuery(
                paths=[path], metadataOnly=True, byPath=True,
                sums={"allocated": {"field": "monthlyCost"}, "largest": {"field": "monthlyCost", "operation": "max"}},
            ), manifest)
            assert metadata["byPath"][path] == {"total": 3, "sums": {"allocated": 10, "largest": 9}}
            values = await project(db, TENANT, SNAPSHOT, RecordQuery(
                paths=[path], facetsOnly=True, facets={"value": "value"}, facetOrder="source", limit=2,
            ), manifest)
            assert [item["value"]["value"] for item in values["items"]] == ["large", "small"]
            assert values["total"] == 3 and values["nextOffset"] == 2
        finally:
            await db.close()
    asyncio.run(run())


def test_real_treemap_aggregates_before_limiting_and_keeps_drilldown():
    async def run():
        db = await connect()
        try:
            await collection(db, TENANT, "costHierarchy", [
                {"subscriptionId": SUB, "subscriptionName": "One", "resourceGroup": "rg-a", "resourceId": "r1", "resourceName": "First", "resourceType": "VM", "monthlySpend": 60},
                {"subscriptionId": SUB, "subscriptionName": "One", "resourceGroup": "rg-b", "resourceId": "r2", "resourceName": "Second", "resourceType": "VM", "monthlySpend": 40},
                {"subscriptionId": "second", "subscriptionName": "Two", "resourceGroup": "rg-c", "resourceId": "r3", "resourceName": "Third", "resourceType": "VM", "monthlySpend": 100},
            ])
            root = await hierarchy(db, TENANT, SNAPSHOT, HierarchyQuery())
            assert root["total"] == 2 and root["attributedTotal"] == 200 and root["sourceCount"] == 3
            groups = await hierarchy(db, TENANT, SNAPSHOT, HierarchyQuery(subscriptionId=SUB))
            assert [item["spend"] for item in groups["items"]] == [60, 40]
            resource = await hierarchy(db, TENANT, SNAPSHOT, HierarchyQuery(subscriptionId=SUB, resourceGroup="rg-a"))
            assert resource["items"][0]["resource"]["resourceId"] == "r1"
            assert resource["attributedTotal"] == 60
        finally:
            await db.close()
    asyncio.run(run())
