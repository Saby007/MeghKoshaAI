import asyncio
from io import BytesIO

import pytest
from openpyxl import load_workbook

from reports.models import CostHierarchyItem
from services import service_retirements
from tests.test_report_exports import _snapshot

RESOURCE = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Insights/webtests/test"


def notice(resource_id=RESOURCE, feature="URL ping test"):
    return {"id": "retirement-1", "properties": {
        "impactedField": "Microsoft.Insights/webtests", "impactedValue": "test",
        "extendedProperties": {"recommendationControl": "ServiceUpgradeAndRetirement", "retirementFeatureName": feature, "retirementDate": "2026-09-30"},
        "resourceMetadata": {"ResourceId": resource_id}, "shortDescription": {"solution": "Review migration."},
    }}


def test_uses_structured_retirement_dates_and_exact_resource_costs():
    snapshot = _snapshot()
    snapshot.report.cost_hierarchy = [CostHierarchyItem(subscriptionId="sub-1", subscriptionName="One", resourceGroup="rg", resourceId=RESOURCE, resourceName="test", resourceType="Microsoft.Insights/webtests", monthlySpend=12, pctOfTotal=.12)]
    rows = service_retirements.retirement_notices(snapshot, "sub-1", [notice(), notice(feature="N/A")])
    assert len(rows) == 1
    assert rows[0].retirement_date == "2026-09-30"
    assert rows[0].matched_cost == 12
    with pytest.raises(ValueError):
        service_retirements.retirement_notices(snapshot, "sub-1", [notice(resource_id=RESOURCE.replace("sub-1", "other"))])


def test_reads_are_bounded_cached_and_exported_without_fabricating_cost(monkeypatch):
    service_retirements._cache.clear()
    calls = []

    async def list_items(path, timeout):
        calls.append(path)
        assert timeout == 8
        assert "SubCategory" in path
        return [notice()]

    monkeypatch.setattr(service_retirements.arm_client, "_list_arm_collection", list_items)
    snapshot = _snapshot()
    summary = asyncio.run(service_retirements.get_retirements(snapshot))
    assert summary.notices[0].matched_cost is None
    assert all(source.available for source in summary.sources)
    asyncio.run(service_retirements.get_retirements(snapshot))
    assert len(calls) == 1
    workbook = load_workbook(BytesIO(service_retirements.export_retirements(summary).content))
    assert workbook["Retirements"]["B2"].value == "2026-09-30"
    assert workbook["Retirements"]["G2"].value is None


def test_failed_retirement_reads_are_unavailable_not_no_retirements(monkeypatch):
    service_retirements._cache.clear()

    async def failed(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(service_retirements.arm_client, "_list_arm_collection", failed)
    summary = asyncio.run(service_retirements.get_retirements(_snapshot()))
    assert summary.notices == []
    assert summary.sources[0].available is False
    assert not service_retirements._cache