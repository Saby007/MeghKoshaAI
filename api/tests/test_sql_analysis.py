from findings.engine import build_report
from datetime import datetime, timedelta, timezone

import pytest

from services.sql_metrics import summarize_metrics, _DATABASE_METRICS
from tests.test_sql_metrics import metric_response


RESOURCE_ID = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Sql/servers/server/databases/app"


def _line(**metadata):
    report = build_report(["sub-1"], 100, {"sql_databases_and_pools": [{
        "id": RESOURCE_ID,
        "name": "app",
        "subscriptionId": "sub-1",
        "type": "microsoft.sql/servers/databases",
        "elasticPoolId": "",
        "skuName": "GP_Gen5_8",
        "skuTier": "GeneralPurpose",
        "licenseType": "LicenseIncluded",
        **metadata,
    }]}, {RESOURCE_ID.lower(): 100})
    return report.tier_a_categories[0].lines[0]


def test_sql_analysis_covers_all_requested_areas_without_inventing_savings():
    line = _line()
    checks = line.sql_context.optimization_checks
    assert [check.rule_id for check in checks] == [f"SQL-R{index:02d}" for index in range(1, 9)]
    assert all(check.next_steps for check in checks)
    assert all(check.estimated_monthly_savings is None for check in checks)
    assert line.monthly_cost == 100


def test_license_included_requires_entitlement_review_not_automatic_ahb():
    checks = {check.rule_id: check for check in _line().sql_context.optimization_checks}
    assert checks["SQL-R04"].status == "review"
    assert "license_entitlement" in checks["SQL-R04"].required_evidence
    assert checks["SQL-R07"].status == "needs_evidence"
    assert "post_optimization_baseline" in checks["SQL-R07"].required_evidence


def test_missing_workload_data_does_not_become_idle_or_rightsize_evidence():
    checks = {check.rule_id: check for check in _line().sql_context.optimization_checks}
    assert checks["SQL-R01"].status == "needs_evidence"
    assert checks["SQL-R06"].status == "needs_evidence"


@pytest.mark.parametrize("maximum, expected", [(20, "review"), (65, "needs_evidence"), (92, "blocked")])
def test_sizing_uses_peak_not_average_and_never_invents_target(maximum, expected):
    now = datetime.now(timezone.utc)
    evidence = summarize_metrics(RESOURCE_ID, _DATABASE_METRICS, metric_response(_DATABASE_METRICS, now, maximum), now)
    checks = {check.rule_id: check for check in _line(sqlWorkloadEvidence=evidence.model_dump(by_alias=True)).sql_context.optimization_checks}
    assert checks["SQL-R01"].status == expected
    assert checks["SQL-R01"].estimated_monthly_savings is None
    assert checks["SQL-R06"].status == "blocked"


@pytest.mark.parametrize("defect", ["stale", "different_resource", "partial", "unexpected_metrics"])
def test_unusable_metrics_cannot_open_a_rightsizing_review(defect):
    now = datetime.now(timezone.utc)
    evidence = summarize_metrics(RESOURCE_ID, _DATABASE_METRICS, metric_response(_DATABASE_METRICS, now), now)
    if defect == "stale":
        evidence.window_end = (now - timedelta(days=40)).isoformat()
    elif defect == "different_resource":
        evidence.resource_id = "/some/other/resource"
    elif defect == "partial":
        evidence.status = "partial"
    else:
        evidence.metrics[0].name = "unrelated"
    checks = {check.rule_id: check for check in _line(sqlWorkloadEvidence=evidence.model_dump(by_alias=True)).sql_context.optimization_checks}
    assert checks["SQL-R01"].status == "needs_evidence"


def test_protection_tag_blocks_retirement():
    checks = {check.rule_id: check for check in _line(tags={"DoNotDelete": "true"}).sql_context.optimization_checks}
    assert checks["SQL-R06"].status == "blocked"


@pytest.mark.parametrize("metadata, rule, expected", [
    ({"licenseType": "BasePrice"}, "SQL-R04", "not_applicable"),
    ({"licenseType": ""}, "SQL-R04", "needs_evidence"),
    ({"skuTier": "Standard"}, "SQL-R04", "not_applicable"),
    ({"skuTier": "BusinessCritical"}, "SQL-R08", "review"),
    ({"skuTier": "GeneralPurpose"}, "SQL-R08", "not_applicable"),
    ({"computeModel": "Serverless"}, "SQL-R02", "not_applicable"),
])
def test_configured_settings_drive_applicability(metadata, rule, expected):
    checks = {check.rule_id: check for check in _line(**metadata).sql_context.optimization_checks}
    assert checks[rule].status == expected