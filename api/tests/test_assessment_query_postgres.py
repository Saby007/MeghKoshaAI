import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from reports.models import CostDetailRow, CostDetailSummary
from services.assessment_queries import CostQuery, cost_groups, daily_costs, execute, persist_costs

pytestmark = pytest.mark.enable_socket
TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "99999999-9999-9999-9999-999999999999"
SUB = TENANT
SECOND_SUB = "22222222-2222-2222-2222-222222222222"
SNAPSHOT = "synthetic-sql-projection"
RESOURCE = f"/subscriptions/{SUB}/resourceGroups/rg-one/providers/Microsoft.Compute/virtualMachines/vm-a"
DATES = [str(date(2026, 7, 1) + timedelta(days=index)) for index in range(63)]


def detail(detail_id, *, resource_id=RESOURCE, owner="team-a", costs=None, subscription=SUB, tags=None):
    return CostDetailRow(
        detailId=detail_id, subscriptionId=subscription, subscriptionName=f"Scope {subscription[:4]}",
        resourceId=resource_id, resourceName=resource_id.rsplit("/", 1)[-1],
        resourceType="Microsoft.Compute/virtualMachines", resourceGroup="RG-One", serviceName="Virtual Machines",
        region="Central India", tags=tags or {"Owner": owner, "Cost_Center": "application-a"},
        tagAttributionSource="exported_resource_tags", dailyCosts=costs or {},
    )


async def connect():
    dsn = os.environ.get("ASSESSMENT_TEST_DSN")
    if not dsn:
        pytest.skip("Set ASSESSMENT_TEST_DSN to an isolated loopback meghkosha_test database.")
    target = urlsplit(dsn)
    if target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.path != "/meghkosha_test":
        pytest.fail("SQL projection tests may only use the dedicated loopback synthetic database.")
    connection = await asyncpg.connect(dsn)
    await connection.execute((Path(__file__).parents[1] / "services" / "assessment_schema.sql").read_text())
    await connection.execute("TRUNCATE assessments.snapshots,assessments.jobs CASCADE")
    for tenant in (TENANT, OTHER_TENANT):
        await connection.execute(
            "INSERT INTO assessments.snapshots "
            "(tenant_id,snapshot_id,scope_hash,subscription_ids,stale_days,created_at,summary,blob_name,published) "
            "VALUES ($1,$2,'synthetic','[]',90,now(),'{}','synthetic',true)",
            tenant, SNAPSHOT,
        )
    rows = [
        detail("one", costs={"2026-07-01": 10, "2026-08-01": 20}),
        detail("two", resource_id=RESOURCE.upper(), owner="team-b",
               costs={"2026-07-01": 2, "2026-08-01": 5}),
        detail("outside-window", owner="not-in-the-selected-window", costs={"2026-09-01": 99}),
        detail("other-resource", resource_id=RESOURCE.replace("vm-a", "vm-b"),
               subscription=SECOND_SUB, costs={"2026-07-01": 0, "2026-08-01": 30}),
    ]
    await persist_costs(connection, TENANT, SNAPSHOT, CostDetailSummary(status="complete", dates=DATES, rows=rows))
    await persist_costs(connection, OTHER_TENANT, SNAPSHOT, CostDetailSummary(
        status="complete", dates=DATES, rows=[detail("private", costs={"2026-08-01": 999999})],
    ))
    return connection


def query(**changes):
    return CostQuery(**{"kind": "groups", "window": {"startDate": "2026-08-01", "endDate": "2026-08-31"}, **changes})


def test_real_sql_complete_group_totals_pages_and_tenant_isolation():
    async def run():
        db = await connect()
        try:
            page = await cost_groups(db, TENANT, SNAPSHOT, query(limit=1), DATES)
            assert page["total"] == 2
            assert page["currentTotal"] == 55
            assert page["previousTotal"] == 12
            assert page["items"][0]["current"] == 30
            assert page["nextOffset"] == 1
            second = await cost_groups(db, TENANT, SNAPSHOT, query(offset=1, limit=1), DATES)
            row = second["items"][0]
            assert row["current"] == 25 and row["previous"] == 12 and row["sourceCount"] == 2
            assert row["percentage"] == pytest.approx(13 / 12 * 100)
            assert row["owners"] == ["team-a", "team-b"]
            assert second["nextOffset"] is None
            assert 999999 not in [item["current"] for item in page["items"] + second["items"]]
        finally:
            await db.close()
    asyncio.run(run())


def test_real_sql_filters_and_daily_calendar_alignment():
    async def run():
        db = await connect()
        try:
            filtered = await cost_groups(
                db, TENANT, SNAPSHOT, query(filters={"resourceGroup": "rg-ONE", "tagKey": "cost-center", "tagValue": "application-a"}), DATES,
            )
            assert filtered["currentTotal"] == 55
            daily = await daily_costs(db, TENANT, SNAPSHOT, query(kind="daily"), DATES)
            first = daily["series"][0]
            assert first["days"][0] == {
                "date": "2026-08-01", "previousDate": "2026-07-01", "current": 25, "previous": 12,
            }
            assert len(first["days"]) == 31
            missing = await cost_groups(db, TENANT, SNAPSHOT, query(), ["2026-08-01"])
            assert all(item["current"] is None and item["previous"] is None for item in missing["items"])
        finally:
            await db.close()
    asyncio.run(run())


def test_real_sql_facets_sources_and_budget_scoping_match_original_helpers():
    async def run():
        db = await connect()
        try:
            facets = await execute(db, TENANT, SNAPSHOT, query(kind="facets", tagPairs=True, tagKey="cost-center", limit=100), DATES)
            assert any(item["field"] == "tagPair" and item["value"] == '["Cost_Center","application-a"]' for item in facets["items"])
            groups = await cost_groups(db, TENANT, SNAPSHOT, query(filters={"resourceId": RESOURCE}), DATES)
            assert groups["spikeCount"] == 1
            sources = await execute(db, TENANT, SNAPSHOT, query(kind="sources", sourceId=groups["items"][0]["id"]), DATES)
            assert sources["total"] == 2
            assert {item["tags"]["Owner"] for item in sources["items"]} == {"team-a", "team-b"}
            budget = {"subscriptionId": SUB, "name": "application", "filter": {
                "tags": {"name": "Cost Center", "operator": "In", "values": ["application-a"]},
            }}
            daily = await execute(db, TENANT, SNAPSHOT, query(kind="budget", budget=budget), DATES)
            assert daily["verified"] and daily["values"][0] == 25 and daily["values"][1] is None
            unknown = await execute(db, TENANT, SNAPSHOT, query(kind="budget", budget={**budget, "filter": {
                "dimensions": {"name": "unsupported", "operator": "In", "values": ["x"]},
            }}), DATES)
            assert not unknown["verified"] and unknown["values"][0] == 25
            scoped = await execute(db, TENANT, SNAPSHOT, query(kind="daily", groupConstraints=[{
                "dimension": "resource", "sourceId": groups["items"][0]["id"],
            }]), DATES)
            assert len(scoped["series"]) == 1 and scoped["series"][0]["days"][0]["current"] == 25
        finally:
            await db.close()
    asyncio.run(run())
