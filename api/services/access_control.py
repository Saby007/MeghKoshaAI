"""Per-user Azure RBAC authorization for onboarded subscriptions.

The Container App queries ARM/Cost Management/Resource Graph under its own managed
identity (see arm_client.py), not the signed-in user's identity - Static Web Apps'
"authenticated" role only proves the caller is a valid Microsoft Entra account in the
tenant, not that they have any Azure RBAC on the subscriptions being reported on.
This module enforces a conservative role policy for subscription-wide operations.
Inherited/custom grants and unrecognized conditions remain unsupported.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Literal

from fastapi import HTTPException

from services import arm_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300.0
_access_cache: dict[tuple[str, str], tuple[bool, float]] = {}

_OWNER_ROLE = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_CONTRIBUTOR_ROLE = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_READER_ROLE = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_COST_CONTRIBUTOR_ROLE = "434105ed-43f6-45c7-a02f-909b2ba83430"
SubscriptionOperation = Literal["report_read", "budget_write", "budget_delete", "export_write", "export_delete", "export_run"]
_OPERATION_ROLES: dict[SubscriptionOperation, frozenset[str]] = {
    "report_read": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _READER_ROLE}),
    "budget_write": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _COST_CONTRIBUTOR_ROLE}),
    "budget_delete": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _COST_CONTRIBUTOR_ROLE}),
    "export_write": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _COST_CONTRIBUTOR_ROLE}),
    "export_delete": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _COST_CONTRIBUTOR_ROLE}),
    "export_run": frozenset({_OWNER_ROLE, _CONTRIBUTOR_ROLE, _COST_CONTRIBUTOR_ROLE}),
}

_ROLE_ASSIGNMENT_UNRELATED_OPERATIONS = frozenset({
    "report_read", "budget_write", "budget_delete", "export_write", "export_delete", "export_run",
})
_GUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_ROLE_IDS_PATTERN = rf"{_GUID_PATTERN}(?:,{_GUID_PATTERN})*"
_OWNER_DELEGATION_TEMPLATE = (
    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'}))"
    "OR(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId]"
    "ForAnyOfAllValues:GuidNotEquals{ROLE_IDS}))"
    "AND"
    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'}))"
    "OR(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId]"
    "ForAnyOfAllValues:GuidNotEquals{ROLE_IDS}))"
)
_OWNER_DELEGATION_CONDITION = re.compile(
    re.escape(_OWNER_DELEGATION_TEMPLATE).replace("ROLE_IDS", _ROLE_IDS_PATTERN),
    re.IGNORECASE,
)


def _allows_owner_delegation_condition(properties: dict, operation: SubscriptionOperation) -> bool:
    condition = properties.get("condition")
    if (
        operation not in _ROLE_ASSIGNMENT_UNRELATED_OPERATIONS
        or properties.get("conditionVersion") != "2.0"
        or not isinstance(condition, str)
    ):
        return False
    # Recognize only Azure's excluded-role delegation template. For these app
    # operations both negated roleAssignments ActionMatches guards are true,
    # so the two OR clauses permit the operation regardless of excluded roles.
    # This is not a general ABAC evaluator; any other expression fails closed.
    normalized = re.sub(r"[ \t\r\n]+", "", condition)
    return _OWNER_DELEGATION_CONDITION.fullmatch(normalized) is not None


def _allows_operation(assignment: dict, subscription_id: str, operation: SubscriptionOperation) -> bool:
    properties = assignment.get("properties") or {}
    expected_scope = f"/subscriptions/{subscription_id}".casefold()
    if str(properties.get("scope") or "").rstrip("/").casefold() != expected_scope:
        return False
    role_definition = str(properties.get("roleDefinitionId") or "").rstrip("/").casefold()
    prefix, separator, role_id = role_definition.rpartition("/providers/microsoft.authorization/roledefinitions/")
    if not separator or prefix not in ("", expected_scope):
        return False
    if properties.get("condition") or properties.get("conditionVersion"):
        if role_id != _OWNER_ROLE or not _allows_owner_delegation_condition(properties, operation):
            return False
    return role_id in _OPERATION_ROLES[operation]


async def _has_access(
    subscription_id: str,
    principal_object_id: str,
    operation: SubscriptionOperation = "report_read",
    *,
    fresh: bool = False,
) -> bool:
    if operation not in _OPERATION_ROLES:
        raise ValueError("Unsupported subscription operation")
    subscription_id = subscription_id.strip().lower()
    principal_object_id = principal_object_id.strip().lower()
    cache_key = (subscription_id, principal_object_id)
    cached = _access_cache.get(cache_key)
    now = time.monotonic()
    if operation == "report_read" and not fresh and cached and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        assignments = await arm_client.list_role_assignments_for_principal(subscription_id, principal_object_id)
        has_access = any(
            _allows_operation(assignment, subscription_id, operation)
            for assignment in assignments
        )
    except Exception as error:
        logger.warning(
            "Role assignment lookup failed for subscription %s / principal %s",
            subscription_id,
            principal_object_id,
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="Subscription authorization is temporarily unavailable. Retry later.") from error
    if operation == "report_read":
        _access_cache[cache_key] = (has_access, now)
    return has_access


async def authorized_subscription_ids(
    principal_object_id: str, subscription_ids: list[str], *, fresh: bool = False,
) -> list[str]:
    """Subscriptions with a supported report-reader grant, preserving input order."""
    if not subscription_ids:
        return []
    results = await asyncio.gather(*(_has_access(sub_id, principal_object_id, fresh=fresh) for sub_id in subscription_ids))
    return [sub_id for sub_id, ok in zip(subscription_ids, results) if ok]


async def require_subscription_access(principal_object_id: str, subscription_id: str) -> None:
    if not await _has_access(subscription_id, principal_object_id):
        raise HTTPException(status_code=403, detail="You do not have Azure access to this subscription")


async def require_subscription_operation(
    principal_object_id: str,
    subscription_id: str,
    operation: SubscriptionOperation,
) -> None:
    if not await _has_access(subscription_id, principal_object_id, operation):
        raise HTTPException(status_code=403, detail="You are not authorized for this subscription operation")


async def require_all_subscription_access(principal_object_id: str, subscription_ids: list[str]) -> None:
    authorized = set(await authorized_subscription_ids(principal_object_id, subscription_ids))
    unauthorized = [sub_id for sub_id in subscription_ids if sub_id not in authorized]
    if unauthorized:
        raise HTTPException(status_code=403, detail="You do not have Azure access to one or more subscriptions in this report")
