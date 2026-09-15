import asyncio
import base64
import json
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault("AI_PROJECT_ENDPOINT", "https://example.test/api/projects/test")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")

import main
from anomalies.models import AnomalySummary
from reports.models import RateOptimizationResponse, RateOptimizationScenario
from services import exchange_rates
from services import report_snapshots
from services.focus_cost_reader import FocusCostData, FocusCostDataError

_AUTH_PAYLOAD = {
    "identityProvider": "aad",
    "userId": "user-1",
    "userDetails": "user@example.test",
    "userRoles": ["authenticated"],
    "claims": [{"typ": "tid", "val": "11111111-1111-1111-1111-111111111111"}],
}
_AUTH_HEADERS = {
    "x-ms-client-principal": base64.b64encode(json.dumps(_AUTH_PAYLOAD).encode()).decode(),
}

client = TestClient(main.app, headers=_AUTH_HEADERS)


def test_network_expansion_queries_are_registered():
    assert {
        "empty_backend_pools",
        "empty_load_balancer_backend_pools",
        "idle_virtual_network_gateways",
        "idle_nat_gateways",
        "idle_expressroute_circuits",
    }.issubset(main._QUERIES)


    assert main._METRIC_NETWORK_CATEGORIES == (
        "idle_virtual_network_gateways",
        "idle_nat_gateways",
        "idle_expressroute_circuits",
    )
    assert {
        "sql_databases_and_pools",
        "sql_managed_instances_and_pools",
        "sql_virtual_machines",
        "compute_ahb_candidates",
        "ai_cognitive_accounts",
        "ai_foundry_projects",
        "ai_ml_workspaces",
        "ai_search_services",
        "unattached_network_interfaces",
        "unassociated_network_security_groups",
        "unassociated_route_tables",
        "empty_availability_sets",
        "deallocated_virtual_machines",
        "zero_instance_vm_scale_sets",
        "empty_app_service_plans",
        "stopped_web_apps",
        "empty_virtual_networks",
        "disconnected_private_endpoints",
        "stopped_aks_clusters",
        "empty_resource_groups",
        "old_custom_images",
    }.issubset(main._QUERIES)


def test_action_write_cutover_pause_does_not_read_or_mutate_saved_state(monkeypatch):
    monkeypatch.setenv("MEGHKOSHA_ACTION_WRITES_PAUSED", "true")

    async def unexpected(*args, **kwargs):
        pytest.fail("Paused action request accessed persisted state")

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", unexpected)
    monkeypatch.setattr(main.finops_actions, "save_action", unexpected)
    response = client.put("/api/report/actions/test-action", json={
        "snapshotId": "a" * 32, "expectedVersion": 0, "status": "open", "owner": "", "note": "Draft",
    })
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert "draft is unchanged" in response.json()["detail"]


def test_stale_days_are_validated_and_rendered_into_queries():
    assert main.AssessmentRequest(subscriptionIds=["sub-1"]).stale_days == 90
    with pytest.raises(ValidationError):
        main.AssessmentRequest(subscriptionIds=["sub-1"], staleDays=45)

    rendered = main._render_queries(30)
    assert "{StaleDays}" not in rendered["old_snapshots"]
    assert "ageDays > 30" in rendered["old_snapshots"]
    assert "ageDays > 30" in rendered["old_custom_images"]


def test_protected_resource_tags_are_excluded_case_insensitively():
    rows = [
        {"id": "keep", "tags": {"DoNotDelete": "true"}},
        {"id": "include", "tags": {"Owner": "finops"}},
        {"id": "untagged", "tags": None},
    ]

    included, excluded = main._exclude_protected_resources(rows)

    assert [row["id"] for row in included] == ["include", "untagged"]
    assert excluded == 1


def test_exchange_rates_endpoint_serializes_provider_contract(monkeypatch):
    async def get_rates(base_currency):
        assert base_currency == "USD"
        return {
            "baseCurrency": "USD",
            "provider": "European Central Bank",
            "providerUrl": "https://www.ecb.europa.eu/",
            "publishedDate": "2026-08-13",
            "fetchedAt": "2026-08-14T06:00:00+00:00",
            "stale": False,
            "rates": {"EUR": 0.866, "USD": 1.0},
            "disclaimer": "Indicative ECB reference rates; not for transaction settlement.",
        }

    monkeypatch.setattr(main.exchange_rate_service, "get_exchange_rates", get_rates)

    response = client.get("/api/exchange-rates?base=USD")

    assert response.status_code == 200
    assert response.json()["baseCurrency"] == "USD"
    assert response.json()["rates"]["USD"] == 1.0


def test_rate_optimization_endpoint_serializes_explicit_scenario(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    async def collect(subscription_ids, subscription_names, scenario):
        assert subscription_ids == ["sub-1"]
        assert subscription_names == {"sub-1": "Sub One"}
        assert scenario == RateOptimizationScenario(
            lookBackPeriod="Last30Days",
            term="P1Y",
            reservationResourceType="VirtualMachines",
        )
        return RateOptimizationResponse(
            scenario=scenario,
            generatedAt="2026-08-16T10:00:00+00:00",
            reservations=[],
            savingsPlans=[],
            sourceStatus=[],
            projectionNotice="Projected values are not aggregate savings.",
        )

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main.rate_optimization, "collect_rate_optimization", collect)

    response = client.post(
        "/api/rate-optimization",
        json={
            "subscriptionIds": ["sub-1"],
            "lookBackPeriod": "Last30Days",
            "term": "P1Y",
            "reservationResourceType": "VirtualMachines",
        },
    )

    assert response.status_code == 200
    assert response.json()["scenario"] == {
        "scope": "Single",
        "lookBackPeriod": "Last30Days",
        "term": "P1Y",
        "reservationResourceType": "VirtualMachines",
    }


def test_rate_optimization_endpoint_rejects_unsupported_scenario():
    response = client.post(
        "/api/rate-optimization",
        json={
            "subscriptionIds": ["sub-1"],
            "lookBackPeriod": "Last90Days",
            "term": "P1Y",
            "reservationResourceType": "VirtualMachines",
        },
    )

    assert response.status_code == 422


def test_subscriptions_endpoint_only_returns_subscriptions_the_caller_has_azure_rbac_on(monkeypatch):
    async def list_subscriptions():
        return [
            {"subscriptionId": "sub-1", "displayName": "Sub One", "state": "Enabled"},
            {"subscriptionId": "sub-2", "displayName": "Sub Two", "state": "Enabled"},
        ]

    async def authorized_subscription_ids(principal_object_id, subscription_ids):
        assert principal_object_id == "22222222-2222-2222-2222-222222222222"
        return [sub_id for sub_id in subscription_ids if sub_id == "sub-1"]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main.access_control, "authorized_subscription_ids", authorized_subscription_ids)

    response = client.get("/api/subscriptions")

    assert response.status_code == 200
    assert [item["subscriptionId"] for item in response.json()] == ["sub-1"]


    def test_storage_onboarding_template_is_additive_and_keeps_last_access_separate(monkeypatch):
        monkeypatch.setenv("COST_CONTROL_PRINCIPAL_ID", "11111111-1111-1111-1111-111111111111")

        template = main._storage_onboarding_template()

        assert [resource["type"] for resource in template["resources"]] == [
            "Microsoft.Storage/storageAccounts/blobServices/containers",
            "Microsoft.Storage/storageAccounts/inventoryPolicies",
            "Microsoft.Authorization/roleAssignments",
        ]
        assert not any(resource["type"].endswith("blobServices") for resource in template["resources"])
        policy = template["resources"][1]["properties"]["policy"]
        assert policy["rules"][0]["definition"]["schedule"] == "Weekly"
        assert "LastAccessTime" in policy["rules"][0]["definition"]["schemaFields"]
        role = template["resources"][2]
        assert "containers/{1}" in role["scope"]
        assert role["properties"]["principalType"] == "ServicePrincipal"
        assert "blob-service-properties update" in template["outputs"]["nextStep"]["value"]


    def test_storage_onboarding_guidance_requires_principal_and_explains_network_billing(monkeypatch):
        monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
        result = asyncio.run(main.get_storage_onboarding_guidance(_snapshot_request()))

        assert "enable-last-access-tracking true" in result["lastAccessCommand"]
        assert "Private-only" in result["networkNotice"]
        assert "billed" in result["billingNotice"]
def test_anomaly_endpoint_loads_complete_history_and_serializes_result(monkeypatch):
    history = object()

    async def load_history(subscription_ids, required_days):
        assert subscription_ids == ["sub-1"]
        assert required_days == 60
        return type("History", (), {"records": history, "currency": "USD"})()

    def detect(records, currency):
        assert records is history
        assert currency == "USD"
        return AnomalySummary(
            algorithmVersion="test-v1",
            label="MeghKoshaAI detection",
            status="ready",
            statusMessage="Ready",
            historyStart="2026-06-01",
            historyEnd="2026-07-31",
            completeDays=61,
            requiredDays=60,
            currency="USD",
            generatedAt="2026-08-16T13:00:00Z",
            trend=[],
            anomalies=[],
        )

    monkeypatch.setattr(main.focus_history_reader, "load_complete_focus_history", load_history)
    monkeypatch.setattr(main, "detect_anomalies", detect)

    response = client.post("/api/anomalies", json={"subscriptionIds": ["sub-1"]})

    assert response.status_code == 200
    assert response.json()["label"] == "MeghKoshaAI detection"
    assert response.json()["completeDays"] == 61


def test_anomaly_endpoint_fails_closed_on_incomplete_history(monkeypatch):
    async def fail(*args, **kwargs):
        raise FocusCostDataError("FOCUS history is missing 1 day")

    monkeypatch.setattr(main.focus_history_reader, "load_complete_focus_history", fail)

    response = client.post("/api/anomalies", json={"subscriptionIds": ["sub-1"]})

    assert response.status_code == 503
    assert "missing 1 day" in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (exchange_rates.UnsupportedCurrencyError("ECB does not publish XYZ"), 422, "ECB does not publish XYZ"),
        (
            exchange_rates.ExchangeRateUnavailableError("ECB unavailable"),
            503,
            "Exchange rates are temporarily unavailable",
        ),
    ],
)
def test_exchange_rates_endpoint_maps_service_errors(monkeypatch, error, status_code, detail):
    async def fail(*args):
        raise error

    monkeypatch.setattr(main.exchange_rate_service, "get_exchange_rates", fail)

    response = client.get("/api/exchange-rates?base=XYZ")

    assert response.status_code == status_code
    assert response.json()["detail"] == detail


def test_report_rejects_incomplete_export_data(monkeypatch):
    async def list_subscriptions():
        return [
            {"subscriptionId": "sub-1", "displayName": "Sub One"},
            {"subscriptionId": "sub-2", "displayName": "Sub Two"},
        ]

    async def load_incomplete_export(subscription_ids):
        raise FocusCostDataError("FocusCost files are missing subscriptions: sub-2")

    async def empty_list(*args):
        return []

    async def empty_counts(*args):
        return {}

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main, "load_latest_complete_focus_costs", load_incomplete_export)
    monkeypatch.setattr(main.arm_client, "list_advisor_recommendations", empty_list)
    monkeypatch.setattr(main.arm_client, "query_resource_graph", empty_list)
    monkeypatch.setattr(main.arm_client, "get_untagged_resource_counts", empty_counts)

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.run_report(main.AssessmentRequest(subscriptionIds=["sub-1", "sub-2"]), _snapshot_request()))

    assert error.value.status_code == 503
    assert error.value.detail["subscriptionIds"] == ["sub-1", "sub-2"]
    assert "missing subscriptions" in error.value.detail["message"]


def test_report_uses_complete_focus_export(monkeypatch):
    published = []

    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    async def load_complete_export(subscription_ids):
        return FocusCostData(
            data_version="1.2-preview",
            period="2026-07",
            period_start="2026-07-01",
            period_end="2026-07-31",
            generated_at="2026-08-12T10:00:00+00:00",
            currency="USD",
            pricing_currencies=["USD"],
            subscription_ids=["sub-1"],
            subscription_names={"sub-1": "Export Sub One"},
            row_count=1,
            billed_cost_by_subscription={"sub-1": 125.5},
            effective_cost_by_subscription={"sub-1": 125.5},
            list_cost_by_subscription={"sub-1": 126.0},
            contracted_cost_by_subscription={"sub-1": 125.5},
            negotiated_discount_by_subscription={"sub-1": 0.5},
            billed_cost_by_resource_id={},
            effective_cost_by_resource_id={},
            service_spend={"Virtual Machines": 125.5},
            service_category_spend={"Compute": 125.5},
            service_family_spend={"Compute": 125.5},
            provider_spend={"Microsoft.Compute": 125.5},
        )

    async def empty_list(*args):
        return []

    async def empty_counts(*args):
        return {}

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main, "load_latest_complete_focus_costs", load_complete_export)
    monkeypatch.setattr(main.arm_client, "list_advisor_recommendations", empty_list)
    monkeypatch.setattr(main.arm_client, "query_resource_graph", empty_list)
    monkeypatch.setattr(main.arm_client, "get_untagged_resource_counts", empty_counts)
    monkeypatch.setattr(
        main.report_snapshots,
        "publish_snapshot",
        lambda snapshot: _record_snapshot(published, snapshot),
    )

    report = asyncio.run(main.run_report(main.AssessmentRequest(subscriptionIds=["sub-1"]), _snapshot_request()))

    assert report.executive_summary.current_monthly_spend == 125.5
    assert report.report_metadata.period == "2026-07"
    assert report.report_metadata.cost_basis == "FocusCost (EffectiveCost)"
    assert report.report_metadata.source == "Private completed FocusCost exports"
    assert report.report_metadata.stale_days == 90
    assert report.report_metadata.protected_tag_keys == ["donotdelete"]
    assert report.report_metadata.excluded_protected_resources == 0
    assert report.completeness.status == "Reconciled"
    assert report.top_services[0].display_name == "Virtual Machines & Compute"
    assert len(report.network_metric_coverage) == 3
    assert all(item.candidate_count == 0 for item in report.network_metric_coverage)
    assert report.pricing_summary.available is True
    assert len(published) == 1
    assert published[0].report.executive_summary.current_monthly_spend == 125.5


async def _record_snapshot(target, snapshot):
    target.append(snapshot)


def test_latest_report_endpoint_requires_tenant_principal_and_serializes_snapshot(monkeypatch):
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(subscription_ids, stale_days):
        assert subscription_ids == ["sub-1"]
        assert stale_days == 90
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", load_latest)
    result = asyncio.run(main.get_latest_report(
        _snapshot_request(),
        subscription_ids=["sub-1"],
        stale_days=90,
    ))

    assert result.snapshot_id == snapshot.snapshot_id
    assert result.report.executive_summary.current_monthly_spend == 100


@pytest.mark.parametrize("requested_threshold", [7, 14, 30, 60, 90, 180, 365])
def test_latest_report_endpoint_accepts_repeated_scope_query_parameters(monkeypatch, requested_threshold):
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(subscription_ids, stale_days):
        assert subscription_ids == ["sub-1", "sub-2"]
        assert stale_days == requested_threshold
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", load_latest)
    response = client.get(
        "/api/report/latest",
        params=[
            ("subscriptionId", "sub-1"),
            ("subscriptionId", "sub-2"),
            ("staleDays", str(requested_threshold)),
        ],
    )

    assert response.status_code == 200
    assert response.json()["snapshotId"] == snapshot.snapshot_id


@pytest.mark.parametrize("threshold", ["45", "90.5", "not-a-number", ""])
def test_latest_report_rejects_invalid_query_threshold_before_snapshot_read(monkeypatch, threshold):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid threshold reached snapshot storage")

    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", unexpected)
    response = client.get("/api/report/latest", params={"subscriptionId": "sub-1", "staleDays": threshold})
    assert response.status_code == 422


def test_budget_summary_retains_tag_filters_and_read_failures_are_not_empty(monkeypatch):
    subscription = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    scope_filter = {"tags": {"name": "Application", "operator": "In", "values": ["Finance"]}}
    summary = main._native_budget_summary(subscription, {"name": "Finance", "properties": {
        "amount": 100, "filter": scope_filter, "currentSpend": {"amount": 105, "unit": "USD"},
    }})
    assert summary.filter == scope_filter
    assert summary.cost_basis == "ActualCost"
    assert summary.scope == f"/subscriptions/{subscription}"

    async def unavailable(subscription_id):
        raise httpx.ReadTimeout("synthetic failure")

    import httpx
    monkeypatch.setattr(main.arm_client, "list_native_budgets", unavailable)
    response = client.get("/api/budgets", params={"subscriptionId": subscription})
    assert response.status_code == 503
    assert "complete requested scope" in response.json()["detail"]


def test_latest_report_endpoint_maps_missing_and_corrupt_snapshots(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)

    async def missing(*args):
        raise report_snapshots.ReportSnapshotNotFoundError("latest.json")

    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", missing)
    with pytest.raises(HTTPException) as missing_error:
        asyncio.run(main.get_latest_report(_snapshot_request(), subscription_ids=None))
    assert missing_error.value.status_code == 404

    async def corrupt(*args):
        raise report_snapshots.ReportSnapshotIntegrityError("digest")

    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", corrupt)
    with pytest.raises(HTTPException) as corrupt_error:
        asyncio.run(main.get_latest_report(_snapshot_request(), subscription_ids=None))
    assert corrupt_error.value.status_code == 503


def test_report_export_uses_selected_snapshot_and_sets_download_headers(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(*args):
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_latest)
    response = asyncio.run(main.export_report(
        _snapshot_request(),
        snapshot_id=snapshot.snapshot_id,
        report_type="executive",
    ))

    assert response.status_code == 200
    assert response.body.startswith(b"%PDF")
    assert response.media_type == "application/pdf"
    assert response.headers["x-report-snapshot-id"] == snapshot.snapshot_id
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].endswith('.pdf"')


def test_report_export_rejects_missing_selected_snapshot(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_missing(snapshot_id):
        assert snapshot_id == "different-snapshot"
        raise report_snapshots.ReportSnapshotNotFoundError(snapshot_id)

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_missing)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.export_report(
            _snapshot_request(),
            snapshot_id="different-snapshot",
            report_type="full",
        ))

    assert error.value.status_code == 404
    assert "not found" in error.value.detail


def test_report_export_full_type_scopes_workbook_to_one_subscription(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(*args):
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_latest)
    response = asyncio.run(main.export_report(
        _snapshot_request(),
        snapshot_id=snapshot.snapshot_id,
        report_type="full",
        subscription_id="sub-1",
    ))

    assert response.status_code == 200
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "sub-1" in response.headers["content-disposition"] or "Sub-One" in response.headers["content-disposition"]


def test_report_export_rejects_subscription_scope_for_non_full_report_types(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(*args):
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_latest)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.export_report(
            _snapshot_request(),
            snapshot_id=snapshot.snapshot_id,
            report_type="executive",
            subscription_id="sub-1",
        ))

    assert error.value.status_code == 422


def test_report_export_rejects_a_subscription_outside_the_report_scope(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report,
        ["sub-1"],
        90,
        created_at="2026-08-17T08:00:00+00:00",
    )

    async def load_latest(*args):
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_latest)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.export_report(
            _snapshot_request(),
            snapshot_id=snapshot.snapshot_id,
            report_type="full",
            subscription_id="sub-not-in-scope",
        ))

    assert error.value.status_code == 422


def test_snapshot_export_downloads_the_requested_immutable_report(monkeypatch):
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report, ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )

    async def load_snapshot(snapshot_id):
        assert snapshot_id == snapshot.snapshot_id
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    response = asyncio.run(main.export_snapshot_report(
        _snapshot_request(),
        snapshot_id=snapshot.snapshot_id,
        report_type="executive",
    ))

    assert response.body.startswith(b"%PDF")
    assert response.headers["x-report-snapshot-id"] == snapshot.snapshot_id


def test_report_email_uses_authenticated_identity_as_the_only_recipient(monkeypatch):
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report, ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )
    captured = {}

    async def load_snapshot(snapshot_id):
        return snapshot

    async def send_report_link(selected, report_type, recipient, actor):
        captured.update(selected=selected, report_type=report_type, recipient=recipient, actor=actor)
        return main.report_email.ReportEmailDelivery("attempt-1", "operation-1", recipient, "Succeeded")

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    monkeypatch.setattr(main.report_email, "send_report_link", send_report_link)
    body = main.ReportEmailRequest(snapshotId=snapshot.snapshot_id, type="full")
    result = asyncio.run(main.email_report(_snapshot_request(), body))

    assert "recipient" not in main.ReportEmailRequest.model_fields
    assert captured["recipient"] == "user@example.test"
    assert captured["actor"] == "11111111-1111-1111-1111-111111111111:user-1"
    assert result == {
        "attemptId": "attempt-1",
        "operationId": "operation-1",
        "recipient": "user@example.test",
        "status": "Succeeded",
    }


def test_snapshot_catalog_and_custom_export_are_authenticated_and_snapshot_bound(monkeypatch):
    report = asyncio.run(_build_latest_snapshot_report())
    snapshot = report_snapshots.build_snapshot(
        report, ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )
    summary = main.ReportSnapshotSummary(
        snapshotId=snapshot.snapshot_id,
        scopeHash=snapshot.scope_hash,
        subscriptionIds=snapshot.subscription_ids,
        staleDays=90,
        createdAt=snapshot.created_at,
        period="2026-07",
        periodStart="2026-07-01",
        periodEnd="2026-07-31",
        currency="USD",
        costBasis="FocusCost (EffectiveCost)",
        reportSchemaVersion="1.0",
    )

    async def list_snapshots(subscription_ids, limit):
        assert subscription_ids == ["sub-1"]
        assert limit == 10
        return [summary]

    async def load_snapshot(snapshot_id):
        assert snapshot_id == snapshot.snapshot_id
        return snapshot

    monkeypatch.setattr(main.report_snapshots, "list_snapshots", list_snapshots)
    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    catalog = asyncio.run(main.get_report_snapshots(
        _snapshot_request(), subscription_ids=["sub-1"], limit=10
    ))
    assert catalog[0].snapshot_id == snapshot.snapshot_id
    response = asyncio.run(main.export_custom_report(
        _snapshot_request(),
        main.CustomReportRequest(snapshotIds=[snapshot.snapshot_id], modules=["summary", "subscriptions"]),
    ))
    assert response.status_code == 200
    assert response.body.startswith(b"PK")
    assert "Custom" in response.headers["content-disposition"]


def test_custom_export_rejects_unknown_module():
    with pytest.raises(ValidationError):
        main.CustomReportRequest(snapshotIds=["snapshot-1"], modules=[])

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.export_custom_report(
            _snapshot_request(),
            main.CustomReportRequest(snapshotIds=["snapshot-1"], modules=["secrets"]),
        ))
    assert error.value.status_code == 422


def test_advisor_score_change_uses_prior_completed_period_only():
    current = asyncio.run(_build_latest_snapshot_report()).model_copy(update={
        "report_metadata": asyncio.run(_build_latest_snapshot_report()).report_metadata.model_copy(update={
            "period": "2026-08",
            "period_start": "2026-08-01",
        }),
        "advisor_score": asyncio.run(_build_latest_snapshot_report()).advisor_score.model_copy(update={
            "available": True,
            "score": 82,
        }),
    })
    previous_report = current.model_copy(update={
        "report_metadata": current.report_metadata.model_copy(update={"period": "2026-07", "period_start": "2026-07-01"}),
        "advisor_score": current.advisor_score.model_copy(update={"score": 77}),
    })
    previous = type("Snapshot", (), {"report": previous_report})()

    updated = main._apply_advisor_score_change(current, previous)

    assert updated.advisor_score.monthly_change == 5
    assert "2026-07" in updated.advisor_score.status
    same_period = type("Snapshot", (), {"report": current})()
    assert main._apply_advisor_score_change(current, same_period).advisor_score.monthly_change is None


def test_finops_action_update_requires_action_in_snapshot_and_stamps_actor(monkeypatch):
    from reports.models import ActionPlanItem

    report = asyncio.run(_build_latest_snapshot_report()).model_copy(update={
        "action_plan": [ActionPlanItem(
            actionId="unattached_disks",
            action="Delete unattached disks",
            savingMonth=25,
            prerequisite="Owner validation",
            affectedSubscriptions=[{"subscriptionId": "sub-1", "subscriptionName": "Sub One"}],
        )],
    })
    snapshot = report_snapshots.build_snapshot(
        report, ["sub-1"], 90, created_at="2026-08-17T08:00:00+00:00"
    )
    saved = []

    async def load_snapshot(snapshot_id):
        assert snapshot_id == snapshot.snapshot_id
        return snapshot

    async def save_action(state, *, expected_version):
        assert expected_version == 0
        committed = state.model_copy(update={"version": 1})
        saved.append(committed)
        return committed

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", load_snapshot)
    monkeypatch.setattr(main.finops_actions, "save_action", save_action)
    result = asyncio.run(main.update_finops_action(
        _snapshot_request(),
        "unattached_disks",
        main.FinOpsActionUpdateRequest(
            snapshotId=snapshot.snapshot_id,
            expectedVersion=0,
            status="completed",
            owner="owner@example.com",
            dueDate="2026-08-31",
            realizedSavingMonth=20,
            note="Validated",
        ),
    ))

    assert result.action_id == "unattached_disks"
    assert result.updated_by == "11111111-1111-1111-1111-111111111111:user-1"
    assert result.completed_at is not None
    assert result.version == 1
    assert saved == [result]

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.update_finops_action(
            _snapshot_request(),
            "missing_action",
            main.FinOpsActionUpdateRequest(snapshotId=snapshot.snapshot_id, status="open", expectedVersion=0),
        ))
    assert error.value.status_code == 404


@pytest.mark.parametrize("failure, status", [(main.finops_actions.ActionConflictError, 409), (main.finops_actions.ActionIntegrityError, 503)])
def test_action_save_conflict_and_integrity_failure_are_explicit(monkeypatch, failure, status):
    from types import SimpleNamespace

    async def snapshot(snapshot_id):
        return SimpleNamespace(scope_hash="scope-1", subscription_ids=["sub-1"], report=SimpleNamespace(action_plan=[SimpleNamespace(action_id="action-1")]))

    async def save(state, *, expected_version):
        assert expected_version == 2
        raise failure("Action changed")

    monkeypatch.setattr(main.report_snapshots, "load_snapshot", snapshot)
    monkeypatch.setattr(main.finops_actions, "save_action", save)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.update_finops_action(_snapshot_request(), "action-1", main.FinOpsActionUpdateRequest(
            snapshotId="snapshot-123", status="open", expectedVersion=2)))
    assert error.value.status_code == status


def test_action_update_requires_an_explicit_observed_version():
    with pytest.raises(ValidationError):
        main.FinOpsActionUpdateRequest(snapshotId="snapshot-123", status="open")


async def _build_latest_snapshot_report():
    from reports.builder import build_full_report

    return build_full_report(
        subscription_ids=["sub-1"],
        subscription_names={"sub-1": "Sub One"},
        per_sub_spend={"sub-1": 100.0},
        resource_graph_rows={},
        cost_by_resource_id={},
        service_family_spend={},
        advisor_recommendations=[],
        untagged_counts={},
    )


def _snapshot_request():
    from tests.test_control_api import FakeRequest

    return FakeRequest()


def test_report_fails_closed_when_focus_collection_fails(monkeypatch):
    inventory_calls = []

    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    async def focus_failure(subscription_ids):
        raise FocusCostDataError("Focus API temporarily unavailable")

    async def empty_list(*args, **kwargs):
        inventory_calls.append("collection")
        return []

    async def empty_counts(*args):
        inventory_calls.append("counts")
        return {}

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main, "load_latest_complete_focus_costs", focus_failure)
    monkeypatch.setattr(main.arm_client, "list_advisor_recommendations", empty_list)
    monkeypatch.setattr(main.arm_client, "query_resource_graph", empty_list)
    monkeypatch.setattr(main.arm_client, "get_untagged_resource_counts", empty_counts)
    monkeypatch.setattr(main.focus_history_reader, "load_available_focus_history", empty_list)

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.run_report(main.AssessmentRequest(subscriptionIds=["sub-1"]), _snapshot_request()))

    assert error.value.status_code == 503
    assert error.value.detail["message"] == "Focus API temporarily unavailable"
    assert inventory_calls == []