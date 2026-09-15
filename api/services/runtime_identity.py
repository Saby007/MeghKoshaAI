"""Explicit runtime managed identity for backend Azure SDK clients."""

import os

from azure.identity import ManagedIdentityCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential

from services.entra_tokens import configured_uuid


def client_id() -> str:
    try:
        return configured_uuid(os.environ.get("AZURE_CLIENT_ID"))
    except (ValueError, TypeError):
        raise RuntimeError("The backend runtime managed identity is not configured in AZURE_CLIENT_ID.") from None


def credential() -> ManagedIdentityCredential:
    return ManagedIdentityCredential(client_id=client_id())


def async_credential() -> AsyncManagedIdentityCredential:
    return AsyncManagedIdentityCredential(client_id=client_id())