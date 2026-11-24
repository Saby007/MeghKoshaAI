import asyncio
import csv
import gzip
import hashlib
from io import BytesIO
from uuid import UUID
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from azure.core.exceptions import ClientAuthenticationError
from fastapi import HTTPException, Request

from services import arm_client, assessment_queries, assessment_store, manual_access
from services import manual_assessment, manual_focus, target_connection, workspace
from services.focus_cost_reader import FocusCostDataError, _REQUIRED_COLUMNS
from services.assessment_settings import settings

SUB = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WORKSPACE = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def write_report(path, rows):
    columns = sorted(_REQUIRED_COLUMNS | {"Tags", "ChargePeriodEnd", "RegionName"})
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for day, amount, overrides in rows:
            row = dict.fromkeys(columns, "")
            row.update({
                "SubAccountId": SUB, "SubAccountName": "Uploaded subscription",
                "BillingCurrency": "USD", "PricingCurrency": "USD",
                "BillingPeriodStart": f"{day[:7]}-01T00:00:00Z",
                "BillingPeriodEnd": f"{day[:7]}-28T00:00:00Z",
                "ChargePeriodStart": f"{day}T00:00:00Z",
                "BilledCost": amount, "EffectiveCost": amount, "ListCost": amount, "ContractedCost": amount,
                "ResourceId": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
                "ResourceName": "vm", "ResourceType": "Microsoft.Compute/virtualMachines",
                "ServiceName": "Virtual Machines", "ServiceCategory": "Compute", "ChargeCategory": "Usage",
                "x_ResourceGroupName": "rg", "Tags": '{"Application":"Example"}',
            })
            row.update(overrides)
            writer.writerow(row)
    return path


@pytest.fixture
def evidence(tmp_path):
    path = write_report(tmp_path / "any-report-name.csv", [
        ("2024-01-03", "10.25", {}), ("2024-08-03", "-2.25", {}),
        ("2025-03-16", "5", {}), ("2025-03-16", "0", {"ResourceId": "", "ResourceName": ""}),
    ])
    return manual_focus.parse_files({path.name: path}, SUB, "2026-11-19T00:00:00Z")


def test_arbitrary_multi_year_charge_range_is_not_a_six_month_or_calendar_path_window(evidence):
    assert evidence.history.history_start == "2024-01-03"
    assert evidence.history.history_end == "2025-03-16"
    assert evidence.history.complete_days == 3
    assert len(evidence.history.records) == 4
    assert sum(row.effective_cost for row in evidence.history.records) == 13
    assert sum(row.effective_cost for row in evidence.history.records) == 13
    assert evidence.costs.effective_cost_by_subscription == {SUB: 5}
    assert evidence.details.dates == ["2024-01-03", "2024-08-03", "2025-03-16"]
    assert evidence.coverage["assessmentMonthPartial"]
    window = assessment_queries.CostWindow(startDate="2024-01-03", endDate="2025-03-16")
    assert len(assessment_queries._dates(window)) > 366


@pytest.mark.parametrize("override", [
    {"SubAccountId": OTHER}, {"BillingCurrency": ""},
    {"EffectiveCost": "NaN"}, {"EffectiveCost": "Infinity"},
    {"ResourceId": f"/subscriptions/{OTHER}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"},
    {"ChargePeriodStart": "not-a-date"},
])
def test_invalid_or_out_of_scope_financial_rows_are_rejected(tmp_path, override):
    path = write_report(tmp_path / "bad.csv", [("2026-01-01", "1", override)])
    with pytest.raises(FocusCostDataError):
        manual_focus.parse_files({path.name: path}, SUB, "now")


def test_azure_focus_1_0_export_uses_x_pricing_currency(tmp_path):
    source = write_report(tmp_path / "source.csv", [("2026-01-02", "4", {})])
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    azure = tmp_path / "azure-export.csv"
    azure.write_text("\n".join([lines[0].replace("PricingCurrency", "x_PricingCurrency"), *lines[1:]]), encoding="utf-8")
    parsed = manual_focus.parse_files({azure.name: azure}, SUB, "now")
    assert parsed.costs.pricing_currencies == ["USD"]
    assert parsed.coverage["rowCount"] == 1

    missing = tmp_path / "missing.csv"
    missing.write_text("\n".join([lines[0].replace("PricingCurrency", "Other"), *lines[1:]]), encoding="utf-8")
    with pytest.raises(FocusCostDataError, match="PricingCurrency"):
        manual_focus.parse_files({missing.name: missing}, SUB, "now")


def test_gzip_and_mixed_currency_validation(tmp_path):
    csv_path = write_report(tmp_path / "input.csv", [("2026-01-01", "1", {})])
    compressed = tmp_path / "arbitrary.data"
    compressed.write_bytes(gzip.compress(csv_path.read_bytes()))
    assert manual_focus.parse_files({"report.csv.gz": compressed}, SUB, "now").coverage["rowCount"] == 1
    compressed.write_bytes(compressed.read_bytes()[:-8])
    with pytest.raises(FocusCostDataError):
        manual_focus.parse_files({"report.csv.gz": compressed}, SUB, "now")
    write_report(csv_path, [("2026-01-01", "1", {}), ("2026-01-02", "1", {"BillingCurrency": "EUR"})])
    with pytest.raises(FocusCostDataError, match="mixed currencies"):
        manual_focus.parse_files({"report.csv": csv_path}, SUB, "now")


def test_focus_only_assessment_never_calls_azure_and_preserves_financial_views(monkeypatch, evidence):
    async def load_files(dataset_id, subscription_id):
        return evidence

    async def connection(subscription_id):
        return target_connection.Connection(WORKSPACE, 1, SUB, "Target")

    async def forbidden(*args, **kwargs):
        pytest.fail("A FOCUS-only assessment called a target Azure API.")

    monkeypatch.setattr(manual_focus, "load", load_files)
    monkeypatch.setattr(target_connection, "require_subscription", connection)
    monkeypatch.setattr(target_connection, "access_token", forbidden)
    monkeypatch.setattr(arm_client, "query_resource_graph", forbidden)
    report, _ = asyncio.run(manual_assessment.build(UUID(OTHER), [SUB], 90))
    assert report.completeness.complete
    assert report.report_metadata.enrichment_status == "not_configured"
    assert report.report_metadata.upload_start == "2024-01-03"
    assert report.executive_summary.current_monthly_spend == 5
    assert report.cost_details.rows == []
    assert report.cost_details.status == "complete"
    from openpyxl import load_workbook
    from reports.exports import build_full_xlsx
    from services import chat_responder, report_snapshots
    snapshot = report_snapshots.build_snapshot(report, [SUB], 90)
    summary = dict(load_workbook(BytesIO(build_full_xlsx(snapshot).content), data_only=True)["Summary"].values)
    assert summary["Monthly spend"] == 5
    assert summary["Estimated wastage"] is None and summary["Potential savings / month"] is None
    answer = chat_responder.answer_question("List unattached disks", report)
    assert answer.intent == "help" and not answer.metrics


def test_expired_optional_sp_does_not_destroy_uploaded_financial_dashboard(monkeypatch, evidence):
    async def load_files(dataset_id, subscription_id):
        return evidence

    async def connection(subscription_id):
        return target_connection.Connection(WORKSPACE, 1, SUB, "Target", WORKSPACE, OTHER, "test-version")

    async def expired():
        raise ClientAuthenticationError("Synthetic expired secret.")

    monkeypatch.setattr(manual_focus, "load", load_files)
    monkeypatch.setattr(target_connection, "require_subscription", connection)
    monkeypatch.setattr(target_connection, "access_token", expired)
    report, _ = asyncio.run(manual_assessment.build(UUID(OTHER), [SUB], 90))
    assert report.completeness.complete
    assert report.report_metadata.enrichment_status == "failed"
    assert report.executive_summary.current_monthly_spend == 5


def test_arm_uses_sp_token_and_rejects_target_mutations(monkeypatch):
    async def token():
        return "synthetic-service-principal-token"

    monkeypatch.setattr(target_connection, "access_token", token)
    assert asyncio.run(arm_client._token()) == "synthetic-service-principal-token"
    for method, path in [
        ("PUT", f"/subscriptions/{SUB}/resourceGroups/rg"),
        ("DELETE", f"/subscriptions/{SUB}/providers/Microsoft.Consumption/budgets/example"),
        ("POST", f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"),
    ]:
        with pytest.raises(ValueError):
            asyncio.run(arm_client._arm_request(method, path))


def test_workspace_capability_and_csrf_are_required_without_a_user_login(monkeypatch):
    capability = "x" * 43

    class Pool:
        async def fetchrow(self, query, identifier):
            return {"capability_hash": hashlib.sha256(capability.encode()).hexdigest()} if str(identifier) == WORKSPACE else None

    async def pool(**kwargs):
        return Pool()

    def request(secret, csrf=""):
        return Request({"type": "http", "method": "PUT", "path": "/api/admin/configuration", "headers": [
            (b"cookie", f"{workspace.COOKIE_NAME}={WORKSPACE}.{secret}".encode()),
            (b"x-csrf-token", csrf.encode()),
        ]})

    monkeypatch.setattr(assessment_store, "pool", pool)
    session = asyncio.run(workspace.resolve(request(capability)))
    assert session.workspace_id == WORKSPACE
    workspace.require_csrf(request(capability, session.csrf_token), session)
    with pytest.raises(HTTPException) as csrf:
        workspace.require_csrf(request(capability), session)
    assert csrf.value.status_code == 403
    with pytest.raises(HTTPException) as forged:
        asyncio.run(workspace.resolve(request("z" * 43)))
    assert forged.value.status_code == 401


def test_configured_scope_is_single_subscription_not_sp_discovery(monkeypatch):
    async def configuration(**kwargs):
        return target_connection.Connection(WORKSPACE, 1, SUB, "Target")

    monkeypatch.setattr(target_connection, "load", configuration)
    assert asyncio.run(manual_access.authorized_subscription_ids(WORKSPACE, [SUB, OTHER])) == [SUB]
    with pytest.raises(HTTPException) as outside:
        asyncio.run(manual_access.require_all_subscription_access(WORKSPACE, [SUB, OTHER]))
    assert outside.value.status_code == 403


def test_target_credentials_are_shared_only_within_one_context_and_closed(monkeypatch):
    created = []
    configuration = target_connection.Connection(WORKSPACE, 7, SUB, "Target", WORKSPACE, OTHER, "version-7")

    class Credential:
        def __init__(self, tenant, client, secret):
            self.arguments = (tenant, client, secret)
            self.closed = False
            created.append(self)

        async def get_token(self, scope):
            assert scope == "https://management.azure.com/.default"
            return SimpleNamespace(token="synthetic-sp-token")

        async def close(self):
            self.closed = True

    monkeypatch.setattr(target_connection, "load", AsyncMock(return_value=configuration))
    monkeypatch.setattr(target_connection, "_read_secret", AsyncMock(return_value="synthetic-secret"))
    monkeypatch.setattr(target_connection, "ClientSecretCredential", Credential)

    async def exercise():
        async with target_connection.context(WORKSPACE, 7):
            assert await asyncio.gather(*(target_connection.access_token() for _ in range(10))) == ["synthetic-sp-token"] * 10
            first_key = target_connection.cache_key("resource")
        async with target_connection.context(OTHER, 7):
            assert target_connection.cache_key("resource") != first_key
    asyncio.run(exercise())
    assert len(created) == 1
    assert created[0].arguments == (WORKSPACE, OTHER, "synthetic-secret") and created[0].closed


@pytest.mark.parametrize("metrics_unavailable", [False, True])
def test_supplied_sp_enables_only_configured_target_enrichment(monkeypatch, evidence, metrics_unavailable):
    configuration = target_connection.Connection(WORKSPACE, 1, SUB, "Target", WORKSPACE, OTHER, "version")
    graph = AsyncMock(return_value=[])
    monkeypatch.setattr(manual_focus, "load", AsyncMock(return_value=evidence))
    monkeypatch.setattr(target_connection, "require_subscription", AsyncMock(return_value=configuration))
    monkeypatch.setattr(target_connection, "access_token", AsyncMock(return_value="synthetic-sp-token"))
    monkeypatch.setattr(arm_client, "query_resource_graph", graph)
    monkeypatch.setattr(arm_client, "list_advisor_recommendations", AsyncMock(return_value=[]))
    monkeypatch.setattr(arm_client, "get_untagged_resource_counts", AsyncMock(return_value={}))
    if metrics_unavailable:
        monkeypatch.setattr(arm_client, "collect_zero_traffic_findings",
                            AsyncMock(side_effect=httpx.ReadTimeout("Synthetic metric outage.")))
    monkeypatch.setattr(arm_client, "collect_storage_account_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(arm_client, "collect_ai_deployment_usage", AsyncMock(return_value={}))
    report, _ = asyncio.run(manual_assessment.build(UUID(OTHER), [SUB], 90))
    assert report.report_metadata.enrichment_status == ("partial" if metrics_unavailable else "complete")
    assert graph.call_count > 1
    assert all(call.args[1] == [SUB] for call in graph.call_args_list)


def test_twenty_uploaded_months_are_not_truncated_to_twelve(tmp_path, monkeypatch):
    months = [f"{2024 + index // 12}-{index % 12 + 1:02d}" for index in range(20)]
    source = write_report(tmp_path / "long.csv", [(f"{month}-03", "1", {}) for month in months])
    evidence = manual_focus.parse_files({source.name: source}, SUB, "2026-11-19T00:00:00Z")
    monkeypatch.setattr(manual_focus, "load", AsyncMock(return_value=evidence))
    monkeypatch.setattr(target_connection, "require_subscription",
                        AsyncMock(return_value=target_connection.Connection(WORKSPACE, 1, SUB, "Target")))
    report, _ = asyncio.run(manual_assessment.build(UUID(OTHER), [SUB], 90))
    assert evidence.history.periods == months
    assert [point.month for point in report.spend_history.months] == months


def test_database_pool_applies_the_configured_deadline_to_driver_and_server(monkeypatch):
    captured = {}
    marker = SimpleNamespace()

    async def create_pool(**kwargs):
        captured.update(kwargs)
        return marker

    monkeypatch.setenv("ASSESSMENT_DB_HOST", "synthetic.postgres.database.azure.com")
    monkeypatch.setenv("ASSESSMENT_DB_NAME", "meghkosha")
    monkeypatch.setenv("ASSESSMENT_DB_USER", "meghkosha_worker")
    monkeypatch.setenv("ASSESSMENT_DB_COMMAND_TIMEOUT_SECONDS", "75")
    monkeypatch.setattr(assessment_store, "_pool", None)
    monkeypatch.setattr(assessment_store, "_pool_lock", None)
    monkeypatch.setattr(assessment_store.asyncpg, "create_pool", create_pool)
    settings.cache_clear()
    try:
        assert asyncio.run(assessment_store.pool()) is marker
        assert captured["command_timeout"] == 75
        assert captured["server_settings"]["statement_timeout"] == "75000"
    finally:
        settings.cache_clear()
