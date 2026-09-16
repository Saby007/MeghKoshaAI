import base64
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from services import entra_tokens
from services.auth import require_tenant_principal


TENANT_ID = "11111111-1111-1111-1111-111111111111"
OBJECT_ID = "22222222-2222-2222-2222-222222222222"
SWA_USER_ID = "app-specific-user-id"
API_CLIENT_ID = "33333333-3333-3333-3333-333333333333"
WEB_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
SUBJECT = "verified-audit-subject"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = jwt.PyJWK.from_dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key())))


@pytest.fixture(autouse=True)
def _authorize_all_subscriptions_by_default(monkeypatch):
    monkeypatch.setenv("AI_PROJECT_ENDPOINT", "https://example.test")


def _token(**overrides):
    now = int(time.time())
    return jwt.encode({
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "aud": API_CLIENT_ID, "tid": TENANT_ID, "oid": OBJECT_ID, "sub": SUBJECT,
        "azp": WEB_CLIENT_ID, "scp": "access_as_user", "ver": "2.0",
        "iat": now, "nbf": now, "exp": now + 3600,
        "preferred_username": "user@example.test", **overrides,
    }, _PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


def _request(*, token=None, **overrides):
    payload = {
        "identityProvider": "aad",
        "userId": SWA_USER_ID,
        "userDetails": "user@example.test",
        "userRoles": ["authenticated"],
        **overrides,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return SimpleNamespace(headers={"x-ms-client-principal": encoded,
                                    entra_tokens.TOKEN_HEADER: f"Bearer {_token() if token is None else token}"})


@pytest.fixture(autouse=True)
def _token_configuration(monkeypatch):
    monkeypatch.setenv("MEGHKOSHA_AI_ENABLED", "false")
    monkeypatch.setenv("MEGHKOSHA_API_CLIENT_ID", API_CLIENT_ID)
    monkeypatch.setenv("MEGHKOSHA_WEB_CLIENT_ID", WEB_CLIENT_ID)
    monkeypatch.setattr(entra_tokens, "signing_key_for_token", lambda token, configuration: _PUBLIC_KEY)


def test_signed_token_resolves_entra_identity_without_manual_enrollment(monkeypatch):
    monkeypatch.delenv("MEGHKOSHA_IDENTITY_BINDINGS", raising=False)
    principal = require_tenant_principal(_request(), TENANT_ID)

    assert principal.entra_object_id == OBJECT_ID
    assert principal.user_id == SUBJECT
    assert principal.user_details == "user@example.test"
    assert principal.tenant_id == TENANT_ID
    assert principal.actor == f"{TENANT_ID}:{SUBJECT}"


def test_browser_oid_cannot_replace_a_missing_api_token():
    request = _request(token="", userId="unbound-user", claims=[
        {"typ": "tid", "val": TENANT_ID},
        {"typ": "oid", "val": OBJECT_ID},
    ])
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(request, TENANT_ID)
    assert error.value.status_code == 401


def test_missing_token_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("MEGHKOSHA_API_CLIENT_ID", raising=False)
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(_request(), TENANT_ID)
    assert error.value.status_code == 503


def test_subscription_endpoint_passes_verified_entra_id_to_authorization(monkeypatch):
    import asyncio
    import main

    monkeypatch.setenv("AZURE_TENANT_ID", TENANT_ID)

    async def list_subscriptions():
        return [{"subscriptionId": "sub-1", "displayName": "Sub One"}]

    async def authorized_subscriptions(principal_object_id, subscription_ids):
        assert principal_object_id == OBJECT_ID
        assert principal_object_id != SWA_USER_ID
        return subscription_ids

    monkeypatch.setattr(main.arm_client, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(main.access_control, "authorized_subscription_ids", authorized_subscriptions)
    result = asyncio.run(main.get_subscriptions(_request()))
    assert [item["subscriptionId"] for item in result] == ["sub-1"]


@pytest.mark.parametrize("setting", ["MEGHKOSHA_API_CLIENT_ID", "MEGHKOSHA_WEB_CLIENT_ID"])
@pytest.mark.parametrize("raw", ["", "invalid", "00000000-0000-0000-0000-000000000000"])
def test_invalid_token_configuration_returns_503_without_exposing_content(monkeypatch, setting, raw):
    monkeypatch.setenv(setting, raw)
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(_request(), TENANT_ID)
    assert error.value.status_code == 503
    assert OBJECT_ID not in error.value.detail
    assert SWA_USER_ID not in error.value.detail


@pytest.mark.parametrize("tenant", ["", "tenant-1", "00000000-0000-0000-0000-000000000000"])
def test_expected_tenant_must_be_a_configured_uuid(monkeypatch, tenant):
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(_request(), tenant)
    assert error.value.status_code == 503


@pytest.mark.parametrize("overrides", [
    {"userRoles": "authenticated"},
    {"userRoles": ["authenticated", {}]},
    {"userRoles": []},
    {"identityProvider": "github"},
    {"userId": {}},
    {"userId": ""},
    {"userId": f" {SWA_USER_ID}"},
    {"userId": "x" * 257},
    {"userDetails": {}},
    {"claims": {}},
    {"claims": ["bad"]},
    {"claims": [{"typ": "tid", "val": []}]},
    {"claims": [{"typ": "tid", "val": ""}]},
    {"claims": [{"typ": "tid", "val": "33333333-3333-3333-3333-333333333333"}]},
    {"claims": [{"typ": "tid", "val": TENANT_ID}, {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": "33333333-3333-3333-3333-333333333333"}]},
])
def test_untrusted_transport_claims_do_not_change_verified_identity(overrides):
    principal = require_tenant_principal(_request(**overrides), TENANT_ID)
    assert principal.entra_object_id == OBJECT_ID
    assert principal.tenant_id == TENANT_ID
    assert principal.user_details == "user@example.test"


@pytest.mark.parametrize("header", [None, "!!invalid!!", "e30=garbage", pytest.param("x" * 65537, id="oversized-header"),
    base64.b64encode(b"null").decode(), base64.b64encode(b"[]").decode(), base64.b64encode(b"false").decode(),
])
def test_invalid_principal_encoding_or_root_is_401(monkeypatch, header):
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(SimpleNamespace(headers={"x-ms-client-principal": header}), TENANT_ID)
    assert error.value.status_code == 401


def test_bearer_identity_does_not_require_swa_transport():
    principal = require_tenant_principal(
        SimpleNamespace(headers={"authorization": f"Bearer {_token()}"}), TENANT_ID,
    )
    assert principal.entra_object_id == OBJECT_ID


@pytest.mark.parametrize("authorization", [None, "", "Basic token", "Bearer", "Bearer ",
    " Bearer token", "Bearer token extra", "Bearer token,other", "Bearer\ttoken",
    pytest.param("Bearer " + "x" * 32769, id="oversized-bearer"),
])
def test_invalid_bearer_transport_is_rejected(authorization):
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(SimpleNamespace(headers={"authorization": authorization}), TENANT_ID)
    assert error.value.status_code == 401


def test_legacy_custom_token_header_is_not_an_authentication_fallback():
    request = _request()
    del request.headers["authorization"]
    request.headers["x-meghkosha-user-token"] = _token()
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(request, TENANT_ID)
    assert error.value.status_code == 401


def test_http_boundary_rejects_duplicate_authorization_headers():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        response = client.get("/api/auth/me", headers=[
            ("authorization", f"Bearer {_token()}"),
            ("authorization", f"Bearer {_token()}"),
        ])
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_profile_fields_and_browser_oid_cannot_override_verified_token(monkeypatch):
    monkeypatch.setenv("MEGHKOSHA_IDENTITY_BINDINGS", "invalid ignored legacy configuration")
    principal = require_tenant_principal(_request(
        objectId="33333333-3333-3333-3333-333333333333",
        claims=[{"typ": "oid", "val": "33333333-3333-3333-3333-333333333333"}],
    ), TENANT_ID)
    assert principal.entra_object_id == OBJECT_ID


def test_expired_token_blocks_before_cached_subscription_access(monkeypatch):
    import asyncio
    import time
    import main

    monkeypatch.setenv("AZURE_TENANT_ID", TENANT_ID)
    assert require_tenant_principal(_request(), TENANT_ID).entra_object_id == OBJECT_ID
    monkeypatch.setattr(main.access_control, "_access_cache", {("sub-1", OBJECT_ID): (True, time.monotonic())})

    async def unexpected_lookup(*args, **kwargs):
        pytest.fail("Expired principal reached subscription data")

    monkeypatch.setattr(main.arm_client, "list_subscriptions", unexpected_lookup)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_subscriptions(_request(token=_token(exp=1))))
    assert error.value.status_code == 401


def test_verified_identity_does_not_itself_grant_subscription_access(monkeypatch):
    import asyncio
    from services import access_control

    principal = require_tenant_principal(_request(), TENANT_ID)
    monkeypatch.setattr(access_control, "_access_cache", {})

    async def no_roles(subscription_id, principal_object_id):
        assert principal_object_id == OBJECT_ID
        return []

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", no_roles)
    assert asyncio.run(access_control._has_access("sub-1", principal.entra_object_id)) is False


def test_all_main_authorization_calls_use_the_entra_field():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "access_control"]
    assert {call.func.attr for call in calls} == {
        "authorized_subscription_ids", "require_subscription_access",
        "require_subscription_operation", "require_all_subscription_access",
    }
    for call in calls:
        assert isinstance(call.args[0], ast.Attribute)
        assert call.args[0].attr == "entra_object_id"


def test_http_boundary_returns_verified_profile_and_never_accepts_headers_without_token():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        response = client.get("/api/auth/config")
        assert response.status_code == 200
        assert response.json() == {"tenantId": TENANT_ID, "apiClientId": API_CLIENT_ID,
                                   "webClientId": WEB_CLIENT_ID, "scope": f"api://{API_CLIENT_ID}/access_as_user"}
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/auth/me", headers=_request(token="").headers).status_code == 401
        response = client.get("/api/auth/me", headers=_request().headers)
        assert response.status_code == 200
        assert response.json() == {"userId": SUBJECT, "userDetails": "user@example.test", "tenantId": TENANT_ID,
                       "features": {"aiNarration": False}}


def test_token_verification_runs_off_the_event_loop_and_only_once(monkeypatch):
    import asyncio
    import main
    from fastapi.testclient import TestClient

    original = main.require_tenant_principal
    calls = []

    def verify(request, tenant_id):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(tenant_id)
        return original(request, tenant_id)

    monkeypatch.setattr(main, "require_tenant_principal", verify)
    with TestClient(main.app) as client:
        assert client.get("/api/auth/me", headers=_request().headers).status_code == 200
    assert calls == [TENANT_ID]


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/onboarding/template"),
    ("POST", "/api/onboarding/template"),
    ("GET", "/api/subscriptions/discovery"),
    ("POST", "/api/subscriptions/discovery/validate"),
])
def test_removed_subscription_onboarding_routes_cannot_be_called(monkeypatch, method, path):
    import main
    from fastapi.testclient import TestClient

    async def unexpected(*args, **kwargs):
        pytest.fail("A removed onboarding endpoint reached Azure")

    monkeypatch.setattr(main.arm_client, "_arm_request", unexpected)
    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", unexpected)
    with TestClient(main.app) as client:
        assert client.request(method, path).status_code == 401
        response = client.request(method, path, headers=_request().headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("upstream_status, expected_status, code", [
    (401, 503, "azure_managed_identity_unavailable"),
    (403, 403, "azure_managed_identity_forbidden"),
])
def test_http_managed_identity_failure_preserves_a_valid_api_identity(monkeypatch, upstream_status, expected_status, code):
    import httpx
    import main
    from fastapi.testclient import TestClient

    async def identity_unavailable(principal):
        assert principal.entra_object_id == OBJECT_ID
        main.user_arm_client._check_response(httpx.Response(
            upstream_status, text="private upstream diagnostic",
            headers={"WWW-Authenticate": 'Bearer error="insufficient_claims", claims="private"'},
        ), principal.tenant_id)

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", identity_unavailable)
    with TestClient(main.app) as client:
        headers = _request().headers
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        response = client.get("/api/schedules", headers=headers)
        assert response.status_code == expected_status
        assert response.json()["detail"]["code"] == code
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Authorization"
        assert "www-authenticate" not in response.headers
        assert "private" not in response.text
        assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_http_schedule_readiness_failures_return_rows_without_creating_access(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    subscription_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other_subscription = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    verified = {"subscriptionId": subscription_id, "displayName": "Configured access", "readAccess": True,
                "costAccess": True, "accessCheckMode": "permissions"}

    async def subscriptions(principal, subscription_ids=None):
        assert principal.entra_object_id == OBJECT_ID
        assert subscription_ids is None
        return [verified, {**verified, "subscriptionId": other_subscription, "displayName": "Throttled access",
                           "readAccess": False, "costAccess": False, "accessIssue": "Permission lookup was throttled."}]

    async def unexpected(*args, **kwargs):
        pytest.fail("Unavailable schedule listing must not call exports or storage")

    monkeypatch.delenv("APP_SCHEDULER_ENABLED", raising=False)
    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", subscriptions)
    monkeypatch.setattr(main.focus_schedules, "_native_request", unexpected)
    with TestClient(main.app) as client:
        headers = _request().headers
        response = client.get("/api/schedules", headers=headers)
        assert response.status_code == 200
        rows = response.json()
        assert [row["subscriptionId"] for row in rows] == [subscription_id, other_subscription]
        assert rows[0]["availability"] == "configuration_unavailable" and rows[0]["costAccess"] is True
        assert rows[1]["availability"] == "access_unavailable" and rows[1]["costAccess"] is False
        assert all(row["state"] == "unknown" and row["latestRun"] is None for row in rows)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Authorization"
        assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_http_export_configuration_requires_fresh_write_authorization_and_explicit_action(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    subscription_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    subscription = {"subscriptionId": subscription_id, "displayName": "Export scope", "readAccess": True, "costAccess": True}
    checks, mutations, previews = [], [], []
    allowed = True

    async def operation(principal_object_id, selected_id, operation):
        assert principal_object_id == OBJECT_ID and operation == "export_write"
        checks.append(selected_id)
        if not allowed or selected_id != subscription_id:
            raise HTTPException(status_code=403, detail="Export write access is not available")

    async def eligible(principal, subscription_ids=None, probe_cost=True):
        assert principal.entra_object_id == OBJECT_ID
        assert subscription_ids == [subscription_id]
        return [subscription]

    async def preview(item):
        assert item == subscription
        previews.append(item)
        return {"subscriptionId": subscription_id, "state": "missing", "canConfigure": True}

    async def configure(item, *, allow_destination_role_assignment):
        assert item == subscription and allow_destination_role_assignment is True
        mutations.append(item)
        return {"subscriptionId": subscription_id, "state": "configured", "created": True}

    monkeypatch.setattr(main.access_control, "require_subscription_operation", operation)
    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", eligible)
    monkeypatch.setattr(main.focus_schedules, "export_configuration", preview)
    monkeypatch.setattr(main.focus_schedules, "configure_export", configure)
    route = f"/api/schedules/{subscription_id}/export"
    with TestClient(main.app) as client:
        assert client.get(route).status_code == 401
        assert client.put(route, json={"allowDestinationRoleAssignment": True}).status_code == 401
        assert not checks and not mutations and not previews
        headers = _request().headers
        response = client.get(route, headers=headers)
        assert response.status_code == 200 and response.json()["state"] == "missing"
        assert not mutations
        assert response.headers["cache-control"] == "no-store" and response.headers["vary"] == "Authorization"
        allowed = False
        assert client.put(route, headers=headers, json={"allowDestinationRoleAssignment": True}).status_code == 403
        assert not mutations
        allowed = True
        response = client.put(route, headers=headers, json={"allowDestinationRoleAssignment": True})
        assert response.status_code == 200 and response.json()["state"] == "configured"
        assert response.headers["cache-control"] == "no-store"
        assert client.put("/api/schedules/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/export", headers=headers,
                          json={"allowDestinationRoleAssignment": True}).status_code == 403
        assert len(mutations) == 1 and len(previews) == 1
        assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert checks == [subscription_id, subscription_id, subscription_id, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]


@pytest.mark.parametrize("body", [
    {}, {"allowDestinationRoleAssignment": False}, {"allowDestinationRoleAssignment": 1},
    {"allowDestinationRoleAssignment": "true"}, {"allowDestinationRoleAssignment": None},
    {"allowDestinationRoleAssignment": True, "storageResourceId": "/injected"},
    {"allowDestinationRoleAssignment": True, "readAccess": True},
    {"allowDestinationRoleAssignment": True, "scheduleStartAt": "2030-09-05T03:00:00Z"},
])
def test_http_export_configuration_rejects_unconfirmed_or_tampered_input_before_azure(monkeypatch, body):
    import main
    from fastapi.testclient import TestClient

    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid export configuration reached Azure or authorization lookup")

    monkeypatch.setattr(main.access_control, "require_subscription_operation", unexpected)
    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", unexpected)
    monkeypatch.setattr(main.focus_schedules, "configure_export", unexpected)
    with TestClient(main.app) as client:
        response = client.put("/api/schedules/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/export", headers=_request().headers, json=body)
    assert response.status_code == 422


def test_http_schedule_selection_is_bound_to_verified_user_and_rechecked_before_save(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    token = _token()
    subscription = {"subscriptionId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "displayName": "Verified cost scope",
                    "tenantId": TENANT_ID, "state": "Enabled", "readAccess": True, "costAccess": True}
    lookups, writes = [], []

    async def eligible(principal, subscription_ids=None, probe_cost=True):
        assert principal.user_assertion == token and principal.entra_object_id == OBJECT_ID
        lookups.append(subscription_ids)
        if subscription_ids is not None and subscription_ids != [subscription["subscriptionId"]]:
            raise HTTPException(status_code=403, detail="Subscription outside verified scope")
        return [subscription]

    async def load(item):
        return main.focus_schedules.schedule_view(item, None)

    async def create(item, actor, start):
        writes.append((item, actor, start))
        return main.focus_schedules.schedule_view(item, None)

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", eligible)
    monkeypatch.setattr(main.focus_schedules, "load", load)
    monkeypatch.setattr(main.focus_schedules, "create", create)
    with TestClient(main.app) as client:
        assert client.get("/api/schedules").status_code == 401
        assert lookups == []
        headers = {"authorization": f"Bearer {token}"}
        response = client.get("/api/schedules", headers=headers)
        assert response.status_code == 200
        assert response.json()[0]["windowMonths"] == 6
        assert response.headers["cache-control"] == "no-store" and response.headers["vary"] == "Authorization"
        body = {"subscriptionId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "scheduleStartAt": "2030-09-05T03:00:00Z"}
        assert client.post("/api/schedules", headers=headers, json=body).status_code == 403
        assert not writes
        body["subscriptionId"] = subscription["subscriptionId"]
        assert client.post("/api/schedules", headers=headers, json={**body, "readAccess": True}).status_code == 422
        assert not writes
        response = client.post("/api/schedules", headers=headers, json=body)
        assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
        assert token not in response.text
    assert writes == [(subscription, OBJECT_ID, "2030-09-05T03:00:00Z")]
    assert lookups == [None, ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"], [subscription["subscriptionId"]]]


@pytest.mark.parametrize("body", [{"subscriptionIds": []}, {"subscriptionIds": [OBJECT_ID, OBJECT_ID]},
    {"subscriptionIds": [OBJECT_ID], "readAccess": True}, {"subscriptionIds": ["injected"]}])
def test_http_bulk_schedule_rejects_untrusted_selection_shape(monkeypatch, body):
    import main
    from fastapi.testclient import TestClient

    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid bulk selection reached eligibility or execution")

    monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", unexpected)
    monkeypatch.setattr(main.focus_schedules, "advance", unexpected)
    with TestClient(main.app) as client:
        response = client.post("/api/schedules/run-all", headers=_request().headers, json=body)
    assert response.status_code == 422


def test_http_schedules_preserve_identity_and_throttling_headers(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    for status, headers in [(401, {"WWW-Authenticate": 'Bearer error="insufficient_claims", claims="test"'}),
                            (503, {"Retry-After": "120"})]:
        async def unavailable(principal):
            raise HTTPException(status_code=status, detail="Schedules unavailable", headers=headers)

        monkeypatch.setattr(main.user_arm_client, "discover_schedule_subscriptions", unavailable)
        with TestClient(main.app) as client:
            response = client.get("/api/schedules", headers=_request().headers)
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert all(response.headers[name] == value for name, value in headers.items())