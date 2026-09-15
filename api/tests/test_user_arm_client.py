import asyncio
import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.identity.aio import ManagedIdentityCredential
from fastapi import HTTPException

from services import user_arm_client
from services.auth import ClientPrincipal

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OBJECT_ID = "22222222-2222-2222-2222-222222222222"
API_CLIENT_ID = "33333333-3333-3333-3333-333333333333"
WEB_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
IDENTITY_CLIENT_ID = "55555555-5555-5555-5555-555555555555"
SUBSCRIPTION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_SUBSCRIPTION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _authorize_all_subscriptions_by_default():
    pass


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("MEGHKOSHA_API_CLIENT_ID", API_CLIENT_ID)
    monkeypatch.setenv("MEGHKOSHA_WEB_CLIENT_ID", WEB_CLIENT_ID)
    monkeypatch.setenv("AZURE_CLIENT_ID", IDENTITY_CLIENT_ID)
    user_arm_client.access_control._access_cache.clear()

    async def assignments(subscription_id, principal_object_id):
        return [{"properties": {
            "scope": f"/subscriptions/{subscription_id}",
            "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7",
        }}]

    monkeypatch.setattr(user_arm_client.access_control.arm_client, "list_role_assignments_for_principal", assignments)


def principal(assertion="verified-user-assertion", object_id=OBJECT_ID):
    return ClientPrincipal("subject", "user@example.test", TENANT_ID, object_id, user_assertion=assertion)


def subscription(subscription_id=SUBSCRIPTION_ID, **overrides):
    return {"subscriptionId": subscription_id, "tenantId": TENANT_ID,
            "displayName": "Pilot", "state": "Enabled", **overrides}


def test_managed_identity_discovery_filters_app_inventory_without_user_token_exchange(monkeypatch):
    from services import access_control

    monkeypatch.setenv("AZURE_CLIENT_ID", IDENTITY_CLIENT_ID)
    calls = []

    class ManagedIdentity:
        def __init__(self, **kwargs):
            assert kwargs == {"client_id": IDENTITY_CLIENT_ID}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("closed")

        async def get_token(self, scope):
            assert scope == user_arm_client.ARM_SCOPE
            calls.append("arm_token")
            return AccessToken("app-managed-identity-token", 9999999999)

    async def authorized(principal_object_id, subscription_ids, **kwargs):
        assert principal_object_id == OBJECT_ID
        assert subscription_ids == [SUBSCRIPTION_ID, OTHER_SUBSCRIPTION]
        assert kwargs == {"fresh": True}
        calls.append("user_authorized")
        return [SUBSCRIPTION_ID]

    def respond(request):
        assert request.headers["authorization"] == "Bearer app-managed-identity-token"
        return httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]})

    def reject_exchange(**kwargs):
        pytest.fail("Backend Azure calls must not exchange a user assertion")

    monkeypatch.setattr(user_arm_client, "ManagedIdentityCredential", ManagedIdentity)
    monkeypatch.setattr(user_arm_client, "OnBehalfOfCredential", reject_exchange, raising=False)
    monkeypatch.setattr(access_control, "authorized_subscription_ids", authorized)
    monkeypatch.setattr(user_arm_client.httpx, "AsyncClient", lambda **kwargs: _HTTP_CLIENT(
        transport=httpx.MockTransport(respond), **kwargs,
    ))

    assert asyncio.run(user_arm_client.discover_subscriptions(principal(""))) == [subscription()]
    assert calls == ["arm_token", "user_authorized", "closed"]


@pytest.fixture
def transport(monkeypatch):
    calls = []
    lifecycle = []

    class ManagedIdentity:
        def __init__(self, **kwargs):
            assert kwargs == {"client_id": IDENTITY_CLIENT_ID}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            lifecycle.append("mi_closed")

        async def get_token(self, scope):
            assert scope == user_arm_client.ARM_SCOPE
            calls.append(scope)
            return AccessToken("app-managed-identity-token", 9999999999)

    monkeypatch.setattr(user_arm_client, "ManagedIdentityCredential", ManagedIdentity)

    def install(handler):
        monkeypatch.setattr(user_arm_client.httpx, "AsyncClient", lambda **kwargs: _HTTP_CLIENT(
            transport=httpx.MockTransport(handler), **kwargs,
        ))
        return calls, lifecycle

    return install


def test_secretless_discovery_paginates_deduplicates_and_closes_credentials(transport):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "management.azure.com"
        assert request.headers["authorization"] == "Bearer app-managed-identity-token"
        if len(requests) == 1:
            return httpx.Response(200, json={"value": [subscription()],
                "nextLink": f"{user_arm_client.ARM_BASE}/subscriptions?page=2"})
        return httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]})

    calls, lifecycle = transport(respond)
    result = asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert [item["subscriptionId"] for item in result] == [SUBSCRIPTION_ID, OTHER_SUBSCRIPTION]
    assert len(requests) == 2
    assert calls == [user_arm_client.ARM_SCOPE]
    assert lifecycle == ["mi_closed"]


def test_shared_app_identity_does_not_share_authorization_between_users(monkeypatch, transport):
    original = user_arm_client.access_control.arm_client.list_role_assignments_for_principal

    async def assignments(subscription_id, principal_object_id):
        return await original(subscription_id, principal_object_id) if principal_object_id == OBJECT_ID else []

    monkeypatch.setattr(user_arm_client.access_control.arm_client, "list_role_assignments_for_principal", assignments)
    calls, _ = transport(lambda request: httpx.Response(200, json={"value": [subscription()]}))
    assert asyncio.run(user_arm_client.discover_subscriptions(principal())) == [subscription()]
    assert asyncio.run(user_arm_client.discover_subscriptions(principal(object_id=WEB_CLIENT_ID))) == []
    assert calls == [user_arm_client.ARM_SCOPE, user_arm_client.ARM_SCOPE]


def test_schedule_listing_only_checks_rows_authorized_for_each_user(monkeypatch, transport):
    original = user_arm_client.access_control.arm_client.list_role_assignments_for_principal
    checked = []

    async def assignments(subscription_id, principal_object_id):
        allowed = SUBSCRIPTION_ID if principal_object_id == OBJECT_ID else OTHER_SUBSCRIPTION
        return await original(subscription_id, principal_object_id) if subscription_id == allowed else []

    async def ready(client, token, tenant_id, subscription_id, *, probe_cost):
        assert probe_cost is False
        checked.append(subscription_id)
        return True

    monkeypatch.setattr(user_arm_client.access_control.arm_client, "list_role_assignments_for_principal", assignments)
    monkeypatch.setattr(user_arm_client, "_has_read_and_cost_access", ready)
    transport(lambda request: httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]}))
    first = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    second = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(object_id=WEB_CLIENT_ID)))
    assert [item["subscriptionId"] for item in first] == [SUBSCRIPTION_ID]
    assert [item["subscriptionId"] for item in second] == [OTHER_SUBSCRIPTION]
    assert checked == [SUBSCRIPTION_ID, OTHER_SUBSCRIPTION]


def test_schedule_listing_authorization_lookup_failure_does_not_return_metadata(monkeypatch, transport):
    async def failed(*args):
        raise RuntimeError("private authorization failure")

    async def unexpected(*args, **kwargs):
        pytest.fail("Readiness cannot be checked before user authorization succeeds")

    monkeypatch.setattr(user_arm_client.access_control.arm_client, "list_role_assignments_for_principal", failed)
    monkeypatch.setattr(user_arm_client, "_has_read_and_cost_access", unexpected)
    transport(lambda request: httpx.Response(200, json={"value": [subscription()]}))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert failure.value.status_code == 503
    assert "private" not in failure.value.detail and "Pilot" not in failure.value.detail


def test_schedule_listing_does_not_request_live_cost_data(transport):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET", "Reading schedules must not submit a Cost Management query"
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription()]})
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"actions": ["*/read"], "notActions": []}]})
        assert request.url.path.endswith("/resourcegroups")
        return httpx.Response(200, json={"value": []})

    transport(respond)
    rows = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert [item["subscriptionId"] for item in rows] == [SUBSCRIPTION_ID]
    assert rows[0]["readAccess"] is True and rows[0]["costAccess"] is True
    assert len(requests) == 3


def test_schedule_actions_require_live_managed_identity_read_and_cost_access(transport):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer app-managed-identity-token"
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]})
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"actions": ["*/read"], "notActions": []}]})
        if request.url.path.endswith("/resourcegroups"):
            return httpx.Response(200, json={"value": []})
        assert request.method == "POST" and request.url.path.endswith("/query")
        assert json.loads(request.content)["dataset"]["granularity"] == "None"
        if OTHER_SUBSCRIPTION in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, json={"properties": {"columns": [], "rows": []}})

    _, lifecycle = transport(respond)
    assert asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [SUBSCRIPTION_ID])) == [
        {**subscription(), "readAccess": True, "costAccess": True, "accessCheckMode": "live"},
    ]
    assert lifecycle == ["mi_closed"]
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [OTHER_SUBSCRIPTION]))
    assert failure.value.status_code == 403
    assert all(request.method == "GET" or request.url.path.endswith("/query") for request in requests)


@pytest.mark.parametrize("excluded", [["Microsoft.Resources/subscriptions/resourceGroups/read"], ["*/read"], ["Microsoft.CostManagement/query/read"]])
def test_schedule_candidates_do_not_treat_excluded_read_actions_as_access(transport, excluded):
    def respond(request):
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription()]})
        assert request.url.path.endswith("/permissions")
        return httpx.Response(200, json={"value": [{"actions": ["*"], "notActions": excluded}]})

    transport(respond)
    rows = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert len(rows) == 1 and rows[0]["subscriptionId"] == SUBSCRIPTION_ID
    assert rows[0]["readAccess"] is False and rows[0]["costAccess"] is False
    assert rows[0]["accessIssue"]


def test_schedule_listing_does_not_infer_access_from_unrecognized_conditional_permissions(transport):
    def respond(request):
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription()]})
        assert request.url.path.endswith("/permissions")
        return httpx.Response(200, json={"value": [{"actions": ["*"], "notActions": [], "condition": "unrecognized", "conditionVersion": "2.0"}]})

    transport(respond)
    rows = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert len(rows) == 1
    assert rows[0]["readAccess"] is False and rows[0]["costAccess"] is False


def test_schedule_selection_rejects_injected_id_before_permissions_or_cost_calls(transport):
    requests = []
    transport(lambda request: requests.append(request) or httpx.Response(200, json={"value": [subscription()]}))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [OTHER_SUBSCRIPTION]))
    assert failure.value.status_code == 403
    assert len(requests) == 1


@pytest.mark.parametrize("status", [403, 429, 500])
def test_schedule_readiness_failure_preserves_other_authorized_rows(transport, status):
    def respond(request):
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]})
        assert request.method == "GET"
        if SUBSCRIPTION_ID in request.url.path:
            return httpx.Response(status, text="private upstream details", headers={"Retry-After": "120"})
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"actions": ["*/read"], "notActions": []}]})
        return httpx.Response(200, json={"value": []})

    _, lifecycle = transport(respond)
    rows = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert [item["subscriptionId"] for item in rows] == [SUBSCRIPTION_ID, OTHER_SUBSCRIPTION]
    assert rows[0]["readAccess"] is False and rows[0]["costAccess"] is False
    assert rows[0]["accessIssue"] and "private" not in rows[0]["accessIssue"]
    assert rows[1]["readAccess"] is True and rows[1]["costAccess"] is True
    assert rows[1]["accessCheckMode"] == "permissions"
    if status == 429:
        assert "Retry after 120 seconds" in rows[0]["accessIssue"]
    assert lifecycle == ["mi_closed"]
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [SUBSCRIPTION_ID]))
    assert failure.value.status_code == (403 if status == 403 else 503)


def test_schedule_list_timeout_is_local_to_the_affected_subscription(monkeypatch, transport):
    async def respond(request):
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]})
        if SUBSCRIPTION_ID in request.url.path:
            await asyncio.Event().wait()
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"actions": ["*/read"], "notActions": []}]})
        return httpx.Response(200, json={"value": []})

    monkeypatch.setattr(user_arm_client, "ACCESS_CHECK_TIMEOUT_SECONDS", 0.02)
    transport(respond)
    rows = asyncio.run(user_arm_client.discover_schedule_subscriptions(principal()))
    assert len(rows) == 2
    assert rows[0]["readAccess"] is False and rows[0]["accessIssue"]
    assert rows[1]["readAccess"] is True


def test_cost_probe_throttling_names_cost_management_and_keeps_longest_retry(transport):
    def respond(request):
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [subscription()]})
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"value": [{"actions": ["*/read"], "notActions": []}]})
        if request.url.path.endswith("/resourcegroups"):
            return httpx.Response(200, json={"value": []})
        assert request.url.path.endswith("/query")
        return httpx.Response(429, headers={"Retry-After": "30", "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "120"})

    transport(respond)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [SUBSCRIPTION_ID]))
    assert failure.value.status_code == 503
    assert failure.value.detail == "Cost Management access check was throttled. Retry after 120 seconds."
    assert failure.value.headers == {"Retry-After": "120"}


@pytest.mark.parametrize("next_link", [
    "https://attacker.example/subscriptions?page=2", "http://management.azure.com/subscriptions",
    "//attacker.example/subscriptions", "/subscriptions/another-scope", "/subscriptions#fragment", 42,
    "/subscriptions?api-version=2022-12-01",
])
def test_pagination_cannot_relay_tokens_outside_requested_collection(transport, next_link):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [subscription()], "nextLink": next_link})

    transport(respond)
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 502
    assert len(calls) == 1
    assert "attacker" not in error.value.detail


@pytest.mark.parametrize("payload", [{}, {"value": {}}, {"value": [None]},
    {"value": [subscription(subscription_id="invalid")]},
    {"value": [subscription(tenantId=None)]}, {"value": [subscription(state="unknown")]},
    {"value": [subscription(), subscription(displayName="conflicting")]},
])
def test_invalid_collections_do_not_return_partial_success(transport, payload):
    transport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 502


def test_foreign_tenant_is_not_discovered_and_disabled_subscription_is_not_selectable(transport):
    transport(lambda request: httpx.Response(200, json={"value": [
        subscription(), subscription(OTHER_SUBSCRIPTION, state="Disabled"),
        subscription("cccccccc-cccc-cccc-cccc-cccccccccccc", tenantId=OBJECT_ID),
    ]}))
    discovered = asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert len(discovered) == 2
    assert user_arm_client.validate_selection(discovered, [SUBSCRIPTION_ID.upper()]) == [subscription()]
    for selected in [[OTHER_SUBSCRIPTION], [SUBSCRIPTION_ID, "cccccccc-cccc-cccc-cccc-cccccccccccc"]]:
        with pytest.raises(HTTPException) as error:
            user_arm_client.validate_selection(discovered, selected)
        assert error.value.status_code == 403


@pytest.mark.parametrize("selected", [[], ["invalid"], [SUBSCRIPTION_ID, SUBSCRIPTION_ID],
    [SUBSCRIPTION_ID, SUBSCRIPTION_ID.upper()], ["00000000-0000-0000-0000-000000000000"]])
def test_selection_shape_fails_closed(selected):
    with pytest.raises(HTTPException) as error:
        user_arm_client.validate_selection([subscription()], selected)
    assert error.value.status_code == 400


@pytest.mark.parametrize("status, expected", [(401, 503), (403, 403), (429, 503), (500, 503), (302, 502)])
def test_upstream_errors_are_distinct_from_empty_discovery(transport, status, expected):
    _, lifecycle = transport(lambda request: httpx.Response(status, headers={"Retry-After": "120"},
                                                           text="private upstream details"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == expected
    assert "private" not in json.dumps(error.value.detail)
    assert lifecycle == ["mi_closed"]
    if status == 429:
        assert error.value.headers["Retry-After"] == "120"


def test_arm_identity_failure_does_not_challenge_the_signed_in_user(transport):
    claims = '{"access_token":{"acrs":{"essential":true,"value":"c1"}}}'
    encoded = base64.b64encode(claims.encode()).decode()
    transport(lambda request: httpx.Response(401, headers={"WWW-Authenticate": (
        f'Bearer authorization_uri="https://attacker.example", error="insufficient_claims", claims="{encoded}"'
    )}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "azure_managed_identity_unavailable"
    assert "WWW-Authenticate" not in (error.value.headers or {})
    assert "attacker" not in json.dumps(error.value.detail)


@pytest.mark.parametrize("value", [None, "", "invalid", "00000000-0000-0000-0000-000000000000"])
def test_missing_runtime_identity_fails_without_falling_back_to_other_credentials(monkeypatch, transport, value):
    calls, _ = transport(lambda request: pytest.fail("Invalid identity configuration must not reach ARM"))
    monkeypatch.setenv("COST_CONTROL_CLIENT_ID", IDENTITY_CLIENT_ID)
    monkeypatch.setenv("MEGHKOSHA_OBO_MANAGED_IDENTITY_CLIENT_ID", IDENTITY_CLIENT_ID)
    if value is None:
        monkeypatch.delenv("AZURE_CLIENT_ID")
    else:
        monkeypatch.setenv("AZURE_CLIENT_ID", value)
    for caller in [principal(), principal("")]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(user_arm_client.discover_subscriptions(caller))
        assert error.value.status_code == 503
        assert "verified-user-assertion" not in error.value.detail
    assert calls == []
    assert "verified-user-assertion" not in repr(principal())


def test_identity_failure_stops_before_arm_access_and_closes_credentials(monkeypatch, transport):
    requests = []
    _, lifecycle = transport(lambda request: requests.append(request))
    credential_type = user_arm_client.ManagedIdentityCredential

    class UnavailableCredential(credential_type):
        async def get_token(self, *args, **kwargs):
            raise ClientAuthenticationError("private token and diagnostic", response=None)

    monkeypatch.setattr(user_arm_client, "ManagedIdentityCredential", UnavailableCredential)
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "azure_managed_identity_unavailable"
    assert "private" not in json.dumps(error.value.detail)
    assert not error.value.headers
    assert requests == []
    assert lifecycle == ["mi_closed"]


@pytest.mark.parametrize("factory_name, credential_type", [
    ("credential", "ManagedIdentityCredential"),
    ("async_credential", "AsyncManagedIdentityCredential"),
])
def test_backend_credential_factories_select_only_the_runtime_identity(monkeypatch, factory_name, credential_type):
    from services import runtime_identity

    calls = []
    credential = object()

    def create(**kwargs):
        calls.append(kwargs)
        return credential

    monkeypatch.setattr(runtime_identity, credential_type, create)
    monkeypatch.setenv("COST_CONTROL_CLIENT_ID", OTHER_SUBSCRIPTION)
    assert getattr(runtime_identity, factory_name)() is credential
    assert calls == [{"client_id": IDENTITY_CLIENT_ID}]
    monkeypatch.delenv("AZURE_CLIENT_ID")
    with pytest.raises(RuntimeError, match="runtime managed identity"):
        getattr(runtime_identity, factory_name)()
    assert len(calls) == 1


def test_inherited_arm_calls_use_the_explicit_runtime_identity(monkeypatch):
    from services import arm_client, runtime_identity

    calls = []

    class Credential:
        async def get_token(self, scope):
            calls.append(scope)
            return AccessToken("runtime-arm-token", 9999999999)

    monkeypatch.setattr(arm_client, "_credential", None)
    monkeypatch.setattr(runtime_identity, "async_credential", Credential)
    assert asyncio.run(arm_client._token()) == "runtime-arm-token"
    assert calls == [user_arm_client.ARM_SCOPE]


def test_discovery_rechecks_a_previously_cached_user_grant(monkeypatch, transport):
    transport(lambda request: httpx.Response(200, json={"value": [subscription()]}))
    assert asyncio.run(user_arm_client.discover_subscriptions(principal())) == [subscription()]

    async def revoked(subscription_id, principal_object_id):
        return []

    monkeypatch.setattr(user_arm_client.access_control.arm_client, "list_role_assignments_for_principal", revoked)
    assert asyncio.run(user_arm_client.discover_subscriptions(principal())) == []
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_schedule_subscriptions(principal(), [SUBSCRIPTION_ID]))
    assert failure.value.status_code == 403


def test_foreign_tenant_cannot_use_the_runtime_identity(transport):
    calls, _ = transport(lambda request: pytest.fail("A foreign tenant must not reach ARM"))
    caller = ClientPrincipal("subject", "foreign@example.test", WEB_CLIENT_ID, OBJECT_ID)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(user_arm_client.discover_subscriptions(caller))
    assert failure.value.status_code == 403
    assert calls == []


@pytest.mark.parametrize("limit, value, payload", [
    ("MAX_RESPONSE_BYTES", 8, {"value": [subscription()]}),
    ("MAX_SUBSCRIPTIONS", 1, {"value": [subscription(), subscription(OTHER_SUBSCRIPTION)]}),
    ("MAX_PAGES", 1, {"value": [subscription()], "nextLink": "/subscriptions?page=2"}),
])
def test_discovery_limits_never_return_a_partial_collection(monkeypatch, transport, limit, value, payload):
    monkeypatch.setattr(user_arm_client, limit, value)
    transport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 502


def test_total_timeout_closes_request_scoped_credentials(monkeypatch, transport):
    async def never_completes(request):
        await asyncio.Event().wait()

    _, lifecycle = transport(never_completes)
    monkeypatch.setattr(user_arm_client, "DISCOVERY_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 503
    assert lifecycle == ["mi_closed"]


@pytest.mark.parametrize("header", [
    'Bearer error="insufficient_claims", claims="invalid!"',
    'Bearer error="insufficient_claims", claims="e30="',
    'Bearer error="insufficient_claims", claims="e30=", claims="e30="',
    'Bearer error="insufficient_claims"',
])
def test_malformed_claims_challenge_is_not_forwarded(transport, header):
    transport(lambda request: httpx.Response(401, headers={"WWW-Authenticate": header}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(user_arm_client.discover_subscriptions(principal()))
    assert error.value.status_code == 503
    assert "WWW-Authenticate" not in (error.value.headers or {})


@pytest.mark.parametrize("token_error", [None, {"error": "invalid_client", "error_description": "private diagnostic"}])
def test_installed_sdk_uses_container_identity_endpoint_without_obo(monkeypatch, transport, token_error):
    token_requests = []
    closed = []
    arm_requests = []
    endpoint = "http://127.0.0.1:40342/msi/token"
    monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint)
    monkeypatch.setenv("IDENTITY_HEADER", "synthetic-identity-header")

    class TokenResponse(AsyncHttpResponse):
        def __init__(self, request):
            super().__init__(request, None)
            self.status_code = 400 if token_error else 200
            self.headers = {"Content-Type": "application/json"}
            self.content_type = "application/json"

        def body(self):
            return json.dumps(token_error or {
                "access_token": "managed-identity-sdk-token", "expires_on": "9999999999",
                "token_type": "Bearer", "resource": user_arm_client.ARM_BASE,
            }).encode()

    class TokenTransport(AsyncHttpTransport):
        async def open(self):
            return None

        async def close(self):
            closed.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()

        async def send(self, request, **kwargs):
            assert request.url.startswith(endpoint + "?")
            assert request.method == "GET"
            assert request.headers["X-IDENTITY-HEADER"] == "synthetic-identity-header"
            query = parse_qs(request.url.split("?", 1)[1])
            assert query["client_id"] == [IDENTITY_CLIENT_ID]
            assert query["resource"] == [user_arm_client.ARM_BASE]
            assert request.body is None
            token_requests.append(request)
            return TokenResponse(request)

    def respond(request):
        arm_requests.append(request)
        assert request.headers["authorization"] == "Bearer managed-identity-sdk-token"
        return httpx.Response(200, json={"value": [subscription()]})

    transport(respond)
    monkeypatch.setattr(user_arm_client, "ManagedIdentityCredential", lambda **kwargs: ManagedIdentityCredential(
        transport=TokenTransport(), retry_total=0, **kwargs,
    ))
    if token_error:
        with pytest.raises(HTTPException) as error:
            asyncio.run(user_arm_client.discover_subscriptions(principal()))
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "azure_managed_identity_unavailable"
        assert not error.value.headers
        assert "private" not in json.dumps(error.value.detail)
        assert arm_requests == []
    else:
        assert asyncio.run(user_arm_client.discover_subscriptions(principal())) == [subscription()]
        assert len(arm_requests) == 1
    assert len(token_requests) == 1
    assert closed