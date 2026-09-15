"""Unit tests for services.access_control - the per-user Azure RBAC gate.

These tests exercise the real implementation (only services.arm_client's ARM call is
mocked), unlike most of the suite where conftest.py's autouse fixture makes every
subscription "authorized" by default so unrelated tests don't need to care about this.
"""

import asyncio

import pytest
from fastapi import HTTPException

from services import access_control

# conftest.py's global autouse fixture patches these same functions (module-level, so
# `main.access_control` and this import are the same object) to be permissive for the
# rest of the suite. Capture the real implementations now, before any test runs, and
# restore them for this file specifically - these tests are what actually exercises them.
_real_authorized_subscription_ids = access_control.authorized_subscription_ids
_real_require_subscription_access = access_control.require_subscription_access
_real_require_all_subscription_access = access_control.require_all_subscription_access
_real_require_subscription_operation = access_control.require_subscription_operation


def _assignment(subscription_id="sub-1", role_id=access_control._READER_ROLE, **properties):
    return {"properties": {
        "scope": f"/subscriptions/{subscription_id}",
        "roleDefinitionId": f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{role_id}",
        **properties,
    }}


@pytest.fixture(autouse=True)
def _use_real_access_control(monkeypatch):
    monkeypatch.setattr(access_control, "authorized_subscription_ids", _real_authorized_subscription_ids)
    monkeypatch.setattr(access_control, "require_subscription_access", _real_require_subscription_access)
    monkeypatch.setattr(access_control, "require_all_subscription_access", _real_require_all_subscription_access)
    monkeypatch.setattr(access_control, "require_subscription_operation", _real_require_subscription_operation)
    access_control._access_cache.clear()
    yield
    access_control._access_cache.clear()


def test_authorized_subscription_ids_filters_to_subscriptions_with_reader_access(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        assert principal_object_id == "user-1"
        return [_assignment(subscription_id)] if subscription_id == "sub-1" else []

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    result = asyncio.run(access_control.authorized_subscription_ids("user-1", ["sub-1", "sub-2"]))

    assert result == ["sub-1"]


def test_authorized_subscription_ids_preserves_input_order_and_handles_empty_input(monkeypatch):
    calls = []

    async def list_role_assignments(subscription_id, principal_object_id):
        calls.append(subscription_id)
        return [_assignment(subscription_id)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    assert asyncio.run(access_control.authorized_subscription_ids("user-1", [])) == []
    assert calls == []

    result = asyncio.run(access_control.authorized_subscription_ids("user-1", ["sub-2", "sub-1"]))
    assert result == ["sub-2", "sub-1"]


def test_authorization_lookup_errors_return_503_without_caching_denial(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        raise RuntimeError("ARM is unavailable")

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.authorized_subscription_ids("user-1", ["sub-1"]))
    assert error.value.status_code == 503
    assert access_control._access_cache == {}


def test_require_subscription_access_raises_403_without_a_role_assignment(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        return []

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))
    assert error.value.status_code == 403


def test_require_subscription_access_rejects_resource_group_only_assignment(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [{
            "properties": {
                "scope": "/subscriptions/sub-1/resourceGroups/one-group",
                "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7",
            },
        }]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))

    assert error.value.status_code == 403


def test_require_subscription_access_succeeds_with_subscription_reader(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))


def test_require_all_subscription_access_raises_403_if_any_subscription_is_unauthorized(monkeypatch):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id)] if subscription_id == "sub-1" else []

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)

    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_all_subscription_access("user-1", ["sub-1", "sub-2"]))
    assert error.value.status_code == 403


def test_access_cache_avoids_a_repeat_role_assignment_lookup(monkeypatch):
    calls = []

    async def list_role_assignments(subscription_id, principal_object_id):
        calls.append(subscription_id)
        return [_assignment(subscription_id)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    access_control._access_cache.clear()

    asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))
    asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))

    assert calls == ["sub-1"]


@pytest.mark.parametrize("operation", ["budget_write", "budget_delete", "export_write", "export_delete", "export_run"])
def test_reader_cannot_perform_subscription_writes_even_with_cached_read_access(monkeypatch, operation):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", operation))
    assert error.value.status_code == 403


@pytest.mark.parametrize("role_id", [access_control._OWNER_ROLE, access_control._CONTRIBUTOR_ROLE, access_control._COST_CONTRIBUTOR_ROLE])
@pytest.mark.parametrize("operation", ["budget_write", "budget_delete", "export_write", "export_delete", "export_run"])
def test_supported_control_role_allows_subscription_operation(monkeypatch, role_id, operation):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id, role_id)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", operation))


@pytest.mark.parametrize("assignment", [
    {},
    _assignment(role_id="custom-role"),
    _assignment(role_id="18d7d88d-d35e-4fb3-a5c3-7773c20a72d9"),
    _assignment(scope="/subscriptions/another-sub"),
    _assignment(scope="/providers/Microsoft.Management/managementGroups/unknown"),
    _assignment(condition="restricted", conditionVersion="2.0"),
    _assignment(roleDefinitionId="acdd72a7-3385-48ef-bd42-f606fba81ae7"),
])
def test_unverified_role_or_scope_is_not_a_reader_grant(monkeypatch, assignment):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [assignment]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_access("user-1", "sub-1"))
    assert error.value.status_code == 403


def test_control_permissions_are_rechecked_after_a_successful_write_check(monkeypatch):
    assignments = [_assignment(role_id=access_control._OWNER_ROLE)]

    async def list_role_assignments(subscription_id, principal_object_id):
        return assignments

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", "budget_write"))
    assignments.clear()
    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", "budget_write"))
    assert error.value.status_code == 403


_OWNER_DELEGATION_CONDITION = """
(
  (!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'}))
  OR
  (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId]
    ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, f58310d9-a9f6-439a-9e8d-f62e7b41a168})
)
AND
(
  (!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'}))
  OR
  (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId]
    ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, f58310d9-a9f6-439a-9e8d-f62e7b41a168})
)
"""


@pytest.mark.parametrize("operation", [
    "report_read", "budget_write", "budget_delete", "export_write", "export_delete", "export_run",
])
@pytest.mark.parametrize("condition", [
    _OWNER_DELEGATION_CONDITION,
    "".join(_OWNER_DELEGATION_CONDITION.split()).lower(),
])
def test_owner_role_assignment_restrictions_do_not_deny_unrelated_operations(monkeypatch, operation, condition):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id, access_control._OWNER_ROLE, condition=condition, conditionVersion="2.0")]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", operation))


@pytest.mark.parametrize("condition", [
    _OWNER_DELEGATION_CONDITION.replace("roleAssignments/write", "exports/write"),
    _OWNER_DELEGATION_CONDITION.replace("roleAssignments/delete", "*"),
    _OWNER_DELEGATION_CONDITION.replace("!(ActionMatches", "(ActionMatches"),
    _OWNER_DELEGATION_CONDITION.replace(" OR", " AND", 1),
    _OWNER_DELEGATION_CONDITION + " AND (@Request[other] StringEquals 'denied')",
    _OWNER_DELEGATION_CONDITION.replace("ForAnyOfAllValues:GuidNotEquals", "GuidEquals"),
    _OWNER_DELEGATION_CONDITION.replace("@Resource", "@Request"),
    _OWNER_DELEGATION_CONDITION.replace("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "not-a-guid"),
    _OWNER_DELEGATION_CONDITION.replace("ROLE", "role") + " unexpected",
    "restricted",
    {"expression": _OWNER_DELEGATION_CONDITION},
])
def test_unrecognized_owner_conditions_still_deny_export_operations(monkeypatch, condition):
    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id, access_control._OWNER_ROLE, condition=condition, conditionVersion="2.0")]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", "export_write"))
    assert error.value.status_code == 403


@pytest.mark.parametrize("properties", [
    {"conditionVersion": None},
    {"conditionVersion": "1.0"},
    {"scope": "/providers/Microsoft.Management/managementGroups/parent"},
    {"scope": "/subscriptions/sub-1/resourceGroups/one-group"},
    {"roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7"},
    {"roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000001"},
])
def test_known_condition_does_not_override_role_scope_or_version_checks(monkeypatch, properties):
    assignment_properties = {"condition": _OWNER_DELEGATION_CONDITION, "conditionVersion": "2.0", **properties}

    async def list_role_assignments(subscription_id, principal_object_id):
        return [_assignment(subscription_id, access_control._OWNER_ROLE, **assignment_properties)]

    monkeypatch.setattr(access_control.arm_client, "list_role_assignments_for_principal", list_role_assignments)
    with pytest.raises(HTTPException) as error:
        asyncio.run(access_control.require_subscription_operation("user-1", "sub-1", "export_write"))
    assert error.value.status_code == 403


def test_owner_delegation_condition_cannot_authorize_role_assignment_mutations():
    properties = {"condition": _OWNER_DELEGATION_CONDITION, "conditionVersion": "2.0"}
    assert not access_control._allows_owner_delegation_condition(properties, "role_assignment_write")
    assert not access_control._allows_owner_delegation_condition(properties, "role_assignment_delete")
