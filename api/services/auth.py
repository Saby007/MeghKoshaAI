"""Principals derived exclusively from validated delegated API bearer tokens."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request
from services.entra_tokens import resolve_user_token


@dataclass(frozen=True)
class ClientPrincipal:
    user_id: str
    user_details: str
    tenant_id: str
    entra_object_id: str
    user_assertion: str = field(default="", repr=False, compare=False)

    @property
    def actor(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


def require_tenant_principal(request: Request, expected_tenant_id: str) -> ClientPrincipal:
    identity = resolve_user_token(request, expected_tenant_id)
    return ClientPrincipal(user_id=identity.subject, user_details=identity.display_name,
                           tenant_id=identity.tenant_id, entra_object_id=identity.object_id,
                           user_assertion=identity.user_assertion)