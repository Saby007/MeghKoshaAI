import asyncio
import base64
import json
import os
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException

os.environ.setdefault("AI_PROJECT_ENDPOINT", "https://example.test/api/projects/test")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("COST_CONTROL_PRINCIPAL_ID", "11111111-1111-1111-1111-111111111111")

import main

_real_require_subscription_operation = main.access_control.require_subscription_operation
_SUBSCRIPTION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_BUDGET_BODY = {
    "subscriptionId": _SUBSCRIPTION_ID,
    "name": "monthly-budget",
    "amount": 100,
    "timeGrain": "Monthly",
    "startDate": "2026-09-01",
}


class FakeRequest:
    def __init__(self, tenant_id="11111111-1111-1111-1111-111111111111", include_claim=True):
        payload = {
            "identityProvider": "aad",
            "userId": "user-1",
            "userDetails": "user@example.test",
            "userRoles": ["authenticated"],
            "claims": [{"typ": "tid", "val": tenant_id}] if include_claim else [],
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self.headers = {"x-ms-client-principal": encoded}


def test_chat_uses_latest_authenticated_snapshot(monkeypatch):
    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    async def load_latest_snapshot(subscription_ids):
        assert subscription_ids == ["sub-1"]
        return SimpleNamespace(snapshot_id="snapshot-123", report=SimpleNamespace())

    expected = main.chat_responder.ChatAnswer(
        intent="score",
        answer="Grounded answer",
        dataAsOf="2026-07",
        disclaimer="No model call.",
    )
    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main.report_snapshots, "load_latest_snapshot", load_latest_snapshot)
    async def respond_to_question(question, report, history):
        assert question == "Show my score"
        assert history == []
        return expected

    monkeypatch.setattr(main.chat_responder, "respond_to_question", respond_to_question)

    answer = asyncio.run(main.chat(
        FakeRequest(),
        main.ChatRequest(question="Show my score"),
    ))

    assert answer == expected


def test_subscription_onboarding_template_and_routes_are_removed():
    assert not hasattr(main, "_onboarding_template")
    assert not hasattr(main, "SubscriptionSelectionRequest")
    routes = {route.path for route in main.app.routes}
    assert not routes.intersection({"/api/onboarding/template", "/api/subscriptions/discovery", "/api/subscriptions/discovery/validate"})
    assert "/api/schedules" in routes
    assert "/api/subscriptions" in routes


def test_schedule_discovery_rejects_unverified_identity_before_azure(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")

    def denied(request, tenant_id):
        raise HTTPException(status_code=403, detail="Token tenant is not authorized")

    async def unexpected():
        pytest.fail("Invalid identity reached Azure")

    monkeypatch.setattr(main, "require_tenant_principal", denied)
    monkeypatch.setattr(main.arm_client, "list_subscriptions", unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_schedules(FakeRequest("33333333-3333-3333-3333-333333333333")))

    assert error.value.status_code == 403


def test_verified_principal_can_receive_an_empty_schedule_candidate_list(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    async def list_subscriptions(principal):
        return []

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", list_subscriptions)

    assert asyncio.run(main.get_schedules(FakeRequest(include_claim=False))) == []


def test_schedule_lifecycle_routes_are_exposed():
    route_methods = {
        (route.path, method)
        for route in main.app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/api/schedules", "POST") in route_methods
    assert ("/api/schedules/{subscription_id}", "PATCH") in route_methods
    assert ("/api/schedules/{subscription_id}", "DELETE") in route_methods
    assert ("/api/schedules/{subscription_id}/run", "POST") in route_methods
    assert ("/api/schedules/run-all", "POST") in route_methods
    assert ("/api/schedules/{subscription_id}/export", "GET") in route_methods
    assert ("/api/schedules/{subscription_id}/export", "PUT") in route_methods


@pytest.mark.parametrize("endpoint", ["get_export_configuration", "configure_schedule_export"])
def test_export_setup_rejects_subscription_reader_before_discovery(monkeypatch, endpoint):
    async def assignments(subscription_id, principal_object_id):
        return [{"properties": {"scope": f"/subscriptions/{subscription_id}",
                               "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7"}}]

    async def unexpected(*args, **kwargs):
        pytest.fail("Read-only user reached export discovery or setup")

    monkeypatch.setattr(main.access_control, "require_subscription_operation", _real_require_subscription_operation)
    monkeypatch.setattr(main.access_control.arm_client, "list_role_assignments_for_principal", assignments)
    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", unexpected)
    kwargs = {"body": main.ExportConfigureRequest(allowDestinationRoleAssignment=True)} if endpoint == "configure_schedule_export" else {}
    with pytest.raises(HTTPException) as error:
        asyncio.run(getattr(main, endpoint)(FakeRequest(), _SUBSCRIPTION_ID, **kwargs))
    assert error.value.status_code == 403


def test_schedules_load_only_managed_identity_read_and_cost_verified_subscriptions(monkeypatch):
    other_subscription = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    lookups = []

    async def subscriptions(principal):
        assert principal.entra_object_id == "22222222-2222-2222-2222-222222222222"
        return [{"subscriptionId": value, "displayName": value, "readAccess": True, "costAccess": True}
            for value in [_SUBSCRIPTION_ID, other_subscription]]

    async def load(subscription):
        lookups.append(subscription["subscriptionId"])
        return main.focus_schedules.schedule_view(subscription, None)

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", subscriptions)
    monkeypatch.setattr(main.focus_schedules, "load", load)
    result = asyncio.run(main.get_schedules(FakeRequest()))

    assert lookups == [_SUBSCRIPTION_ID, other_subscription]
    assert len(result) == 2
    assert all(row["readAccess"] and row["costAccess"] and row["windowMonths"] == 6 for row in result)


def test_schedule_discovery_failure_is_not_an_empty_result(monkeypatch):
    async def subscriptions(principal):
        raise httpx.ReadTimeout("unavailable")

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", subscriptions)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_schedules(FakeRequest()))
    assert error.value.status_code == 503
    assert "discovery" in error.value.detail


def test_malformed_export_is_unavailable_without_losing_other_authorized_rows(monkeypatch):
    async def subscriptions(principal):
        return [{"subscriptionId": _SUBSCRIPTION_ID, "displayName": "Verified", "readAccess": True, "costAccess": True}]

    async def export(subscription_id):
        raise HTTPException(status_code=503, detail="Preconfigured export required")

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", subscriptions)
    monkeypatch.setattr(main.focus_schedules, "_configuration", lambda: ("tenant", "identity", "https://teststore.blob.core.windows.net"))
    monkeypatch.setattr(main.focus_schedules, "verify_export", export)
    result = asyncio.run(main.get_schedules(FakeRequest()))
    assert result[0]["state"] == "unknown"
    assert result[0]["availability"] == "export_unavailable"


def test_schedule_status_lookups_are_bounded(monkeypatch):
    active = 0
    peak = 0

    async def subscriptions(principal):
        return [{"subscriptionId": f"aaaaaaaa-aaaa-aaaa-aaaa-{index:012d}", "displayName": "Verified"} for index in range(12)]

    async def export(subscription_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return None

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", subscriptions)
    monkeypatch.setattr(main.focus_schedules, "load", export)
    assert len(asyncio.run(main.get_schedules(FakeRequest()))) == 12
    assert peak == 4


def test_schedule_history_read_failure_is_explicit(monkeypatch):
    async def history(subscription_id):
        raise ValueError("Malformed timestamp")

    async def verified(principal, subscription_ids, probe_cost=True):
        return [{"subscriptionId": _SUBSCRIPTION_ID, "displayName": "Verified"}]

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", verified)
    monkeypatch.setattr(main.focus_schedules, "history", history)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_schedule_runs(FakeRequest(), _SUBSCRIPTION_ID))
    assert error.value.status_code == 503
    assert "Execution history" in error.value.detail


@pytest.mark.parametrize("lookup_error, expected_status", [(False, 403), (True, 503)])
@pytest.mark.parametrize("endpoint, kwargs", [
    ("create_budget", {"body": main.BudgetWriteRequest(**_BUDGET_BODY)}),
    ("update_budget", {"subscription_id": _SUBSCRIPTION_ID, "budget_name": "monthly-budget", "body": main.BudgetWriteRequest(**_BUDGET_BODY)}),
    ("remove_budget", {"subscription_id": _SUBSCRIPTION_ID, "budget_name": "monthly-budget"}),
])
def test_subscription_writes_stop_before_azure_calls_for_readers_or_lookup_failures(
    monkeypatch, endpoint, kwargs, lookup_error, expected_status
):
    async def list_role_assignments(subscription_id, principal_object_id):
        assert principal_object_id == "22222222-2222-2222-2222-222222222222"
        if lookup_error:
            raise RuntimeError("Authorization service unavailable")
        return [{"properties": {
            "scope": f"/subscriptions/{subscription_id}",
            "roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{main.access_control._READER_ROLE}",
        }}]

    async def unexpected_azure_call(*args, **kwargs):
        pytest.fail("Denied request reached an Azure operation")

    monkeypatch.setattr(main.access_control, "require_subscription_operation", _real_require_subscription_operation)
    monkeypatch.setattr(main.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    for name in ("get_subscription", "get_native_budget", "put_native_budget", "delete_native_budget"):
        monkeypatch.setattr(main.arm_client, name, unexpected_azure_call)
    for name in ("create_export", "update_export", "delete_export", "run_export", "run_all_exports"):
        monkeypatch.setattr(main.focus_export_control, name, unexpected_azure_call)

    with pytest.raises(HTTPException) as error:
        asyncio.run(getattr(main, endpoint)(FakeRequest(), **kwargs))
    assert error.value.status_code == expected_status


def test_bulk_run_checks_entire_scope_before_starting_any_export(monkeypatch):
    denied_subscription = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    checked = []

    async def verify(principal, subscription_ids, probe_cost=True):
        checked.extend(subscription_ids)
        raise HTTPException(status_code=403, detail="A selected subscription is not eligible")

    async def unexpected_run(*args, **kwargs):
        pytest.fail("Bulk run started before all subscriptions were authorized")

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", verify)
    monkeypatch.setattr(main.focus_schedules, "advance", unexpected_run)

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.run_all_schedules(
            FakeRequest(),
            main.RunAllExportsRequest(subscriptionIds=[_SUBSCRIPTION_ID, denied_subscription]),
        ))
    assert error.value.status_code == 403
    assert checked == [_SUBSCRIPTION_ID, denied_subscription]


@pytest.mark.parametrize("status", [403, 503])
@pytest.mark.parametrize("endpoint, kwargs", [
    ("create_schedule", {"body": main.ScheduleCreateRequest(subscriptionId=_SUBSCRIPTION_ID, scheduleStartAt="2030-09-05T03:00:00Z")}),
    ("update_schedule", {"subscription_id": _SUBSCRIPTION_ID, "body": main.ScheduleStateRequest(state="paused")}),
    ("delete_schedule", {"subscription_id": _SUBSCRIPTION_ID}),
    ("run_schedule_now", {"subscription_id": _SUBSCRIPTION_ID}),
    ("get_schedule_runs", {"subscription_id": _SUBSCRIPTION_ID}),
])
def test_every_schedule_action_rechecks_eligibility_before_any_work(monkeypatch, status, endpoint, kwargs):
    async def denied(principal, subscription_ids, probe_cost=True):
        assert subscription_ids == [_SUBSCRIPTION_ID]
        raise HTTPException(status_code=status, detail="Eligibility denied or unavailable")

    async def unexpected(*args, **kwargs):
        pytest.fail("Unverified subscription reached schedule storage or execution")

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", denied)
    for name in ("create", "update", "advance", "history"):
        monkeypatch.setattr(main.focus_schedules, name, unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(getattr(main, endpoint)(FakeRequest(), **kwargs))
    assert error.value.status_code == status


def test_create_uses_server_verified_subscription_not_client_metadata(monkeypatch):
    async def verified(principal, subscription_ids, probe_cost=True):
        assert subscription_ids == [_SUBSCRIPTION_ID]
        return [{"subscriptionId": _SUBSCRIPTION_ID, "displayName": "Verified name"}]

    async def create(subscription, actor, schedule_start):
        assert subscription == {"subscriptionId": _SUBSCRIPTION_ID, "displayName": "Verified name"}
        assert actor == "22222222-2222-2222-2222-222222222222"
        assert schedule_start == "2030-09-05T03:00:00Z"
        return {"windowMonths": 6}

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", verified)
    monkeypatch.setattr(main.focus_schedules, "create", create)
    result = asyncio.run(main.create_schedule(FakeRequest(), main.ScheduleCreateRequest(
        subscriptionId=_SUBSCRIPTION_ID, scheduleStartAt="2030-09-05T03:00:00Z")))
    assert result == {"windowMonths": 6}