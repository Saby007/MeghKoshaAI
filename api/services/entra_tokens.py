"""Validate delegated user tokens issued for this API, never Graph or ARM tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import os
import threading
import time
from uuid import UUID

import jwt
from fastapi import HTTPException

TOKEN_HEADER = "authorization"
REQUIRED_SCOPE = "access_as_user"
MAX_TOKEN_LENGTH = 32768


@dataclass(frozen=True)
class TokenIdentity:
    tenant_id: str
    object_id: str
    subject: str
    display_name: str
    user_assertion: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class IdentityConfiguration:
    tenant_id: str
    api_client_id: str
    web_client_id: str


def configured_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a UUID string")
    parsed = UUID(value)
    if not parsed.int:
        raise ValueError("A nonzero UUID is required")
    return str(parsed)


def identity_configuration(expected_tenant_id: str) -> IdentityConfiguration:
    try:
        return IdentityConfiguration(
            tenant_id=configured_uuid(expected_tenant_id),
            api_client_id=configured_uuid(os.environ.get("MEGHKOSHA_API_CLIENT_ID")),
            web_client_id=configured_uuid(os.environ.get("MEGHKOSHA_WEB_CLIENT_ID")),
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Server identity configuration is unavailable.") from error


class _TenantSigningKeys(jwt.PyJWKClient):
    def __init__(self, tenant_id: str, api_client_id: str):
        super().__init__(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys?appid={api_client_id}",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
            timeout=3,
        )
        self._expected_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._refresh_lock = threading.Lock()
        self._last_attempt = float("-inf")

    def fetch_data(self):
        with self._refresh_lock:
            now = time.monotonic()
            if now - self._last_attempt < 30:
                cached = self.jwk_set_cache.get() if self.jwk_set_cache else None
                if cached is not None:
                    return cached
                raise jwt.PyJWKClientConnectionError("Signing-key discovery is temporarily unavailable")
            self._last_attempt = now
            try:
                data = super().fetch_data()
                keys = data.get("keys") if isinstance(data, dict) else None
                if not isinstance(keys, list):
                    raise ValueError("Invalid signing-key document")
                accepted = [key for key in keys if isinstance(key, dict)
                            and key.get("kty") == "RSA" and key.get("use") == "sig"
                            and key.get("alg", "RS256") == "RS256"
                            and key.get("issuer") in (self._expected_issuer, "https://login.microsoftonline.com/{tenantid}/v2.0")]
                if not accepted:
                    raise ValueError("No trusted tenant signing keys")
                data = {"keys": accepted}
                if self.jwk_set_cache:
                    self.jwk_set_cache.put(data)
                return data
            except (jwt.PyJWKClientError, ValueError, TypeError) as error:
                if self.jwk_set_cache:
                    self.jwk_set_cache.put(None)
                raise jwt.PyJWKClientConnectionError("Signing-key discovery is temporarily unavailable") from error


@lru_cache(maxsize=4)
def _signing_keys(tenant_id: str, api_client_id: str) -> _TenantSigningKeys:
    return _TenantSigningKeys(tenant_id, api_client_id)


def signing_key_for_token(token: str, configuration: IdentityConfiguration) -> jwt.PyJWK:
    try:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str) or not key_id or len(key_id) > 256:
            raise ValueError("Invalid signing-key header")
        return _signing_keys(configuration.tenant_id, configuration.api_client_id).get_signing_key_from_jwt(token)
    except jwt.PyJWKClientConnectionError as error:
        raise HTTPException(status_code=503, detail="Identity verification is temporarily unavailable. Retry later.") from error
    except (jwt.PyJWKClientError, jwt.InvalidTokenError, ValueError, TypeError) as error:
        raise HTTPException(status_code=401, detail="The delegated API token is invalid or expired.") from error


def bearer_token(request) -> str:
    headers = request.headers
    values = headers.getlist(TOKEN_HEADER) if hasattr(headers, "getlist") else [headers.get(TOKEN_HEADER)]
    authorization = values[0] if len(values) == 1 else None
    if isinstance(authorization, str) and len(authorization) <= MAX_TOKEN_LENGTH + 7:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and separator and token and not any(
            character.isspace() or character == "," for character in token
        ):
            return token
    raise HTTPException(status_code=401, detail="A valid delegated API bearer token is required.",
                        headers={"WWW-Authenticate": "Bearer"})


def resolve_user_token(request, expected_tenant_id: str) -> TokenIdentity:
    configuration = identity_configuration(expected_tenant_id)
    token = bearer_token(request)
    return validate_user_token(
        token,
        tenant_id=configuration.tenant_id,
        api_client_id=configuration.api_client_id,
        web_client_id=configuration.web_client_id,
        signing_key=signing_key_for_token(token, configuration),
    )


def validate_user_token(
    token: str,
    *,
    tenant_id: str,
    api_client_id: str,
    web_client_id: str,
    signing_key: jwt.PyJWK,
) -> TokenIdentity:
    try:
        tenant_id = configured_uuid(tenant_id)
        api_client_id = configured_uuid(api_client_id)
        web_client_id = configured_uuid(web_client_id)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Server identity configuration is unavailable.") from error
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise HTTPException(status_code=401, detail="A valid delegated API token is required.")
    try:
        claims = jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            audience=api_client_id,
            issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            leeway=60,
            options={
                "require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "sub", "azp", "scp", "ver"],
                "strict_aud": True,
            },
        )
        token_tenant = configured_uuid(claims["tid"])
        object_id = configured_uuid(claims["oid"])
        authorized_client = configured_uuid(claims["azp"])
        subject = claims["sub"]
        if claims["ver"] != "2.0" or not isinstance(subject, str) or not subject.strip() or len(subject) > 256:
            raise ValueError("Invalid user token claims")
    except (jwt.InvalidTokenError, ValueError, TypeError) as error:
        raise HTTPException(status_code=401, detail="The delegated API token is invalid or expired.") from error
    if token_tenant != tenant_id or authorized_client != web_client_id:
        raise HTTPException(status_code=403, detail="The token tenant or client is not authorized for this API.")
    scopes = claims["scp"]
    if claims.get("idtyp") == "app" or not isinstance(scopes, str) or REQUIRED_SCOPE not in scopes.split():
        raise HTTPException(status_code=403, detail="Delegated user access to this API is required.")
    display_name = claims.get("preferred_username") or claims.get("name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 320:
        display_name = object_id
    return TokenIdentity(tenant_id=tenant_id, object_id=object_id, subject=subject,
                         display_name=display_name, user_assertion=token)