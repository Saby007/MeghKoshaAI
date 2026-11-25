"""Workspace-scoped, optional Reader credentials for target Azure API calls."""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import ClientSecretCredential
from azure.keyvault.secrets.aio import SecretClient
from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr, model_validator

from services import assessment_store, runtime_identity, workspace

logger = logging.getLogger(__name__)
ARM_BASE = "https://management.azure.com"


class EnrichmentNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class Connection:
    workspace_id: str
    revision: int
    subscription_id: str
    subscription_name: str
    target_tenant_id: str | None = None
    client_id: str | None = None
    secret_version: str | None = None

    @property
    def has_credentials(self) -> bool:
        return self.secret_version is not None

    def public(self) -> dict:
        return {
            "revision": self.revision, "subscriptionId": self.subscription_id,
            "subscriptionName": self.subscription_name, "tenantId": self.target_tenant_id,
            "clientId": self.client_id, "hasCredentials": self.has_credentials,
        }


class ConnectionInput(BaseModel):
    subscription_id: UUID = Field(alias="subscriptionId")
    tenant_id: UUID | None = Field(default=None, alias="tenantId")
    client_id: UUID | None = Field(default=None, alias="clientId")
    client_secret: SecretStr | None = Field(default=None, alias="clientSecret", min_length=1, max_length=4096)
    clear_credentials: bool = Field(default=False, alias="clearCredentials")

    @model_validator(mode="after")
    def credentials_are_complete(self):
        # Tenant is optional: when omitted it is detected from the target subscription.
        supplied = (self.client_id is not None, self.client_secret is not None)
        if any(supplied) and not all(supplied):
            raise ValueError("Provide client ID and client secret together, or omit both.")
        if self.tenant_id is not None and not all(supplied):
            raise ValueError("A tenant ID can only be supplied with a client ID and client secret.")
        if self.clear_credentials and (any(supplied) or self.tenant_id is not None):
            raise ValueError("Choose either credential replacement or credential removal.")
        if self.subscription_id.int == 0 or self.tenant_id is not None and self.tenant_id.int == 0 \
                or self.client_id is not None and self.client_id.int == 0:
            raise ValueError("Identifiers must be nonzero UUIDs.")
        return self


_revision: ContextVar[int | None] = ContextVar("target_configuration_revision", default=None)
_connection: ContextVar[Connection | None] = ContextVar("target_connection", default=None)
@dataclass
class CredentialState:
    credential: ClientSecretCredential | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_credentials: ContextVar[CredentialState | None] = ContextVar("target_credentials", default=None)


@asynccontextmanager
async def context(workspace_id: str, revision: int | None = None):
    namespace_token = workspace.current.set(workspace_id)
    revision_token = _revision.set(revision)
    connection_token = _connection.set(None)
    state = CredentialState()
    credential_token = _credentials.set(state)
    try:
        yield
    finally:
        try:
            if state.credential is not None:
                await state.credential.close()
        finally:
            _credentials.reset(credential_token)
            _connection.reset(connection_token)
            _revision.reset(revision_token)
            workspace.current.reset(namespace_token)


async def load(*, required: bool = True) -> Connection | None:
    cached = _connection.get()
    if cached is not None:
        return cached
    row = await (await assessment_store.pool()).fetchrow(
        "SELECT c.* FROM assessments.workspaces w JOIN assessments.workspace_connections c "
        "ON c.workspace_id=w.workspace_id AND c.revision=coalesce($2,w.configuration_revision) "
        "WHERE w.workspace_id=$1 AND w.expires_at>now()",
        UUID(workspace.namespace()), _revision.get(),
    )
    if row is None:
        if required:
            raise HTTPException(409, "Set the target subscription in Admin before uploading a FOCUS report.")
        return None
    connection = Connection(
        workspace_id=str(row["workspace_id"]), revision=row["revision"],
        subscription_id=row["subscription_id"], subscription_name=row["subscription_name"],
        target_tenant_id=row["target_tenant_id"], client_id=row["client_id"],
        secret_version=row["secret_version"],
    )
    _connection.set(connection)
    return connection


def _vault_url() -> str:
    value = os.environ.get("WORKSPACE_VAULT_URL", "").rstrip("/")
    if not value.startswith("https://") or not value.endswith(".vault.azure.net"):
        raise RuntimeError("The private workspace Key Vault is not configured.")
    return value


async def _store_secret(value: SecretStr) -> str:
    async with runtime_identity.async_credential() as identity:
        async with SecretClient(vault_url=_vault_url(), credential=identity) as vault:
            secret = await vault.set_secret(
                f"focus-sp-{workspace.namespace()}", value.get_secret_value(),
                content_type="MeghKoshaAI target Reader service principal",
            )
    if not secret.properties.version:
        raise RuntimeError("Key Vault did not return a secret version.")
    return secret.properties.version


async def _read_secret(connection: Connection) -> str:
    if not connection.secret_version:
        raise EnrichmentNotConfigured("Azure enrichment is not configured for this uploaded report.")
    async with runtime_identity.async_credential() as identity:
        async with SecretClient(vault_url=_vault_url(), credential=identity) as vault:
            secret = await vault.get_secret(f"focus-sp-{connection.workspace_id}", connection.secret_version)
    if not secret.value:
        raise RuntimeError("The configured service-principal secret is empty.")
    return secret.value


_TENANT_CHALLENGE = re.compile(r"login\.(?:microsoftonline\.com|windows\.net)/([0-9a-fA-F-]{36})", re.IGNORECASE)


async def _discover_tenant(subscription_id: UUID) -> UUID:
    """Azure answers an unauthenticated subscription read with a challenge that names the owning tenant."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(f"{ARM_BASE}/subscriptions/{subscription_id}?api-version=2022-12-01")
    except httpx.HTTPError:
        raise HTTPException(502, "The tenant could not be detected from Azure. Check connectivity or enter the tenant ID.") from None
    match = _TENANT_CHALLENGE.search(response.headers.get("www-authenticate", "")) if response.status_code == 401 else None
    try:
        tenant = UUID(match.group(1)) if match else None
    except ValueError:
        tenant = None
    if tenant is None or tenant.int == 0:
        raise HTTPException(422, "The tenant could not be detected for this subscription. Enter the tenant ID.")
    return tenant


_AAD_ERROR = re.compile(r"(AADSTS\d{3,7})\s*:?\s*([^\r\n]*)")


def _azure_error_summary(failure: Exception) -> str:
    """Return Azure's AADSTS code and its first sentence; the secret is never part of these messages."""
    match = _AAD_ERROR.search(str(failure))
    if match is None:
        return ""
    description = re.split(r"\.\s|Trace ID:", match.group(2), maxsplit=1)[0].strip()[:200]
    return f" Azure error {match.group(1)}" + (f": {description}." if description else ".")


async def _validate(body: ConnectionInput) -> str:
    if body.tenant_id is None or body.client_id is None or body.client_secret is None:
        raise ValueError("Complete credentials are required for an Azure connection test.")
    try:
        async with ClientSecretCredential(
            str(body.tenant_id), str(body.client_id), body.client_secret.get_secret_value(),
        ) as credential:
            token = await credential.get_token(f"{ARM_BASE}/.default")
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.get(
                    f"{ARM_BASE}/subscriptions/{body.subscription_id}?api-version=2022-12-01",
                    headers={"Authorization": f"Bearer {token.token}"},
                )
        if response.status_code in {401, 403, 404}:
            raise HTTPException(422, "The service principal cannot read the target subscription. Verify its Reader grant.")
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("subscriptionId", "")).lower() != str(body.subscription_id):
            raise HTTPException(502, "Azure returned a different subscription than the configured target.")
        return str(payload.get("displayName") or body.subscription_id)
    except ClientAuthenticationError as failure:
        summary = _azure_error_summary(failure)
        logger.warning("Target service-principal validation failed for workspace %s.%s", workspace.namespace(), summary)
        raise HTTPException(
            422, f"Service-principal authentication failed.{summary} Verify tenant, client ID and secret.",
        ) from None
    except (httpx.HTTPError, ValueError):
        logger.warning("Target subscription validation was unavailable for workspace %s", workspace.namespace())
        raise HTTPException(502, "The target subscription could not be validated. Check connectivity and retry.") from None


async def save(body: ConnectionInput) -> Connection:
    previous = await load(required=False)
    subscription = str(body.subscription_id)
    name = subscription
    tenant = client_id = version = None
    if body.client_secret is not None:
        if body.tenant_id is None:
            body = body.model_copy(update={"tenant_id": await _discover_tenant(body.subscription_id)})
        name = await _validate(body)
        tenant, client_id = str(body.tenant_id), str(body.client_id)
        version = await _store_secret(body.client_secret)
    elif previous and previous.subscription_id == subscription and not body.clear_credentials:
        name, tenant, client_id, version = (
            previous.subscription_name, previous.target_tenant_id, previous.client_id, previous.secret_version,
        )
    db = await assessment_store.pool(for_read=False)
    async with db.acquire(timeout=10) as connection, connection.transaction():
        row = await connection.fetchrow(
            "UPDATE assessments.workspaces SET configuration_revision=configuration_revision+1 "
            "WHERE workspace_id=$1 AND expires_at>now() RETURNING configuration_revision",
            UUID(workspace.namespace()),
        )
        if row is None:
            raise HTTPException(401, "The report workspace expired. Reload the application.")
        revision = row["configuration_revision"]
        await connection.execute(
            "UPDATE assessments.jobs SET status='cancelled',phase='configuration_changed',completed_at=now() "
            "WHERE tenant_id=$1 AND task_kind='assessment' AND status IN ('queued','running')",
            workspace.namespace(),
        )
        await connection.execute(
            "INSERT INTO assessments.workspace_connections "
            "(workspace_id,revision,subscription_id,subscription_name,target_tenant_id,client_id,secret_version) "
            "VALUES($1,$2,$3,$4,$5,$6,$7)",
            UUID(workspace.namespace()), revision, subscription, name, tenant, client_id, version,
        )
    result = Connection(workspace.namespace(), revision, subscription, name, tenant, client_id, version)
    _connection.set(result)
    return result


async def access_token() -> str:
    connection = await load()
    if connection is None or not connection.has_credentials:
        raise EnrichmentNotConfigured("Service-principal credentials are absent; Azure enrichment is unavailable.")
    state = _credentials.get()
    if state is None:
        raise RuntimeError("Target Azure calls require a workspace execution context.")
    async with state.lock:
        if state.credential is None:
            if connection.target_tenant_id is None or connection.client_id is None:
                raise RuntimeError("The saved service-principal configuration is incomplete.")
            state.credential = ClientSecretCredential(
                connection.target_tenant_id, connection.client_id, await _read_secret(connection),
            )
    return (await state.credential.get_token(f"{ARM_BASE}/.default")).token


async def require_subscription(subscription_id: str) -> Connection:
    connection = await load()
    if connection is None or subscription_id.lower() != connection.subscription_id:
        raise HTTPException(403, "This subscription is outside the active uploaded-report workspace.")
    return connection


def cache_key(value: str) -> str:
    configuration = _connection.get()
    revision = configuration.revision if configuration else _revision.get()
    return f"{workspace.namespace()}:{revision}:{value}"
