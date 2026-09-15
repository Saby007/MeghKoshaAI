import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from services.entra_tokens import validate_user_token

TENANT_ID = "11111111-1111-4111-8111-111111111111"
API_CLIENT_ID = "22222222-2222-4222-8222-222222222222"
WEB_CLIENT_ID = "33333333-3333-4333-8333-333333333333"
OBJECT_ID = "44444444-4444-4444-8444-444444444444"
OTHER_ID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(autouse=True)
def _authorize_all_subscriptions_by_default():
    """Token validation runs without application imports or authorization bypasses."""


@pytest.fixture(scope="module")
def key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.PyJWK.from_dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())))
    return private_key, public_jwk


def claims(**overrides):
    now = int(time.time())
    return {
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "aud": API_CLIENT_ID, "tid": TENANT_ID, "oid": OBJECT_ID,
        "sub": "verified-pairwise-subject", "azp": WEB_CLIENT_ID, "scp": "access_as_user",
        "ver": "2.0", "iat": now, "nbf": now, "exp": now + 3600,
        "preferred_username": "display@example.test", **overrides,
    }


def validate(token, key_pair, **configuration):
    return validate_user_token(token, tenant_id=TENANT_ID, api_client_id=API_CLIENT_ID,
                               web_client_id=WEB_CLIENT_ID, signing_key=key_pair[1], **configuration)


def test_signed_token_resolves_object_and_audit_subject_without_manual_bindings(key_pair, monkeypatch):
    monkeypatch.delenv("MEGHKOSHA_IDENTITY_BINDINGS", raising=False)
    identity = validate(jwt.encode(claims(), key_pair[0], algorithm="RS256"), key_pair)
    assert identity.object_id == OBJECT_ID
    assert identity.subject == "verified-pairwise-subject"
    assert identity.tenant_id == TENANT_ID
    assert identity.display_name == "display@example.test"


@pytest.mark.parametrize("overrides,status", [
    ({"aud": OTHER_ID}, 401),
    ({"aud": [API_CLIENT_ID, OTHER_ID]}, 401),
    ({"iss": f"https://login.microsoftonline.com/{OTHER_ID}/v2.0"}, 401),
    ({"tid": OTHER_ID}, 403),
    ({"azp": OTHER_ID}, 403),
    ({"scp": "User.Read"}, 403),
    ({"scp": ["access_as_user"]}, 403),
    ({"idtyp": "app"}, 403),
    ({"ver": "1.0"}, 401),
    ({"oid": "not-an-object-id"}, 401),
    ({"oid": "00000000-0000-0000-0000-000000000000"}, 401),
    ({"sub": ""}, 401),
    ({"sub": " "}, 401),
    ({"sub": ["invalid"]}, 401),
    ({"exp": 1}, 401),
    ({"nbf": 9999999999}, 401),
    ({"iat": 9999999999}, 401),
])
def test_wrong_actor_scope_identity_and_validity_are_rejected(key_pair, overrides, status):
    token = jwt.encode(claims(**overrides), key_pair[0], algorithm="RS256")
    with pytest.raises(HTTPException) as error:
        validate(token, key_pair)
    assert error.value.status_code == status
    assert token not in error.value.detail


@pytest.mark.parametrize("missing", ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "sub", "azp", "scp", "ver"])
def test_required_claims_cannot_be_omitted_including_id_tokens_without_scope(key_pair, missing):
    payload = claims()
    del payload[missing]
    with pytest.raises(HTTPException) as error:
        validate(jwt.encode(payload, key_pair[0], algorithm="RS256"), key_pair)
    assert error.value.status_code == 401


def test_forged_signature_and_algorithm_confusion_fail_closed(key_pair):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in [jwt.encode(claims(), other_key, algorithm="RS256"), jwt.encode(claims(), "attacker-key-that-is-at-least-32-bytes-long", algorithm="HS256"), jwt.encode(claims(), None, algorithm="none")]:
        with pytest.raises(HTTPException) as error:
            validate(token, key_pair)
        assert error.value.status_code == 401


@pytest.mark.parametrize("token", ["", "invalid", pytest.param("x" * 32769, id="oversized-token")])
def test_invalid_token_encoding_and_size_are_rejected(key_pair, token):
    with pytest.raises(HTTPException) as error:
        validate(token, key_pair)
    assert error.value.status_code == 401


def test_display_claims_do_not_determine_authorization_identity(key_pair):
    token = jwt.encode(claims(preferred_username=OTHER_ID, name="Another user"), key_pair[0], algorithm="RS256")
    identity = validate(token, key_pair)
    assert identity.object_id == OBJECT_ID
    assert identity.display_name == OTHER_ID


def test_invalid_server_configuration_is_not_reported_as_user_denial(key_pair):
    token = jwt.encode(claims(), key_pair[0], algorithm="RS256")
    with pytest.raises(HTTPException) as error:
        validate_user_token(token, tenant_id=TENANT_ID, api_client_id="", web_client_id=WEB_CLIENT_ID, signing_key=key_pair[1])
    assert error.value.status_code == 503


def test_request_boundary_uses_verified_token_not_browser_profile(key_pair, monkeypatch):
    import base64
    from types import SimpleNamespace
    from services import entra_tokens
    from services.auth import require_tenant_principal

    monkeypatch.setenv("MEGHKOSHA_API_CLIENT_ID", API_CLIENT_ID)
    monkeypatch.setenv("MEGHKOSHA_WEB_CLIENT_ID", WEB_CLIENT_ID)
    monkeypatch.setattr(entra_tokens, "signing_key_for_token", lambda token, configuration: key_pair[1])
    payload = {"identityProvider": "aad", "userId": "app-specific-user", "userDetails": "different@example.test",
               "userRoles": ["authenticated"], "claims": [{"typ": "oid", "val": OTHER_ID}]}
    token = jwt.encode(claims(), key_pair[0], algorithm="RS256")
    request = SimpleNamespace(headers={"x-ms-client-principal": base64.b64encode(json.dumps(payload).encode()).decode(),
                                       entra_tokens.TOKEN_HEADER: f"Bearer {token}"})
    principal = require_tenant_principal(request, TENANT_ID)
    assert principal.entra_object_id == OBJECT_ID
    assert principal.user_id == "verified-pairwise-subject"
    assert principal.user_details == "display@example.test"
    del request.headers[entra_tokens.TOKEN_HEADER]
    with pytest.raises(HTTPException) as error:
        require_tenant_principal(request, TENANT_ID)
    assert error.value.status_code == 401


def test_key_discovery_filters_foreign_issuers_and_bounds_unknown_key_refresh(key_pair, monkeypatch):
    from services import entra_tokens

    trusted = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key_pair[0].public_key()))
    trusted.update(kid="trusted-key", use="sig", issuer=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0")
    foreign = {**trusted, "kid": "foreign-key", "issuer": f"https://login.microsoftonline.com/{OTHER_ID}/v2.0"}
    requests = []

    def fetch_data(client):
        requests.append(client.uri)
        return {"keys": [trusted, foreign]}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_data)
    client = entra_tokens._TenantSigningKeys(TENANT_ID, API_CLIENT_ID)
    assert client.get_signing_key("trusted-key").key_id == "trusted-key"
    for key_id in ["foreign-key", "unknown-key", "unknown-key-2"]:
        with pytest.raises(jwt.PyJWKClientError):
            client.get_signing_key(key_id)
    assert len(requests) == 1
    assert requests[0] == f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys?appid={API_CLIENT_ID}"


def test_key_discovery_failure_is_unavailable_not_an_authentication_success(key_pair, monkeypatch):
    from services import entra_tokens

    configuration = entra_tokens.IdentityConfiguration(TENANT_ID, API_CLIENT_ID, WEB_CLIENT_ID)
    entra_tokens._signing_keys.cache_clear()

    def unavailable(client):
        raise jwt.PyJWKClientConnectionError("synthetic network failure")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unavailable)
    token = jwt.encode(claims(), key_pair[0], algorithm="RS256", headers={"kid": "trusted-key"})
    with pytest.raises(HTTPException) as error:
        entra_tokens.signing_key_for_token(token, configuration)
    assert error.value.status_code == 503
    assert "synthetic" not in error.value.detail
    entra_tokens._signing_keys.cache_clear()