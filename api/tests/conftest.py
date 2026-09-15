"""Shared test fixtures.

By default, every test is granted access to whatever subscriptions it asks for -
most tests are about report/business logic, not authorization, so they should not
need to know about services.access_control. The authorization checks themselves are
covered directly in test_access_control.py and in the dedicated deny-path tests in
test_main.py / test_control_api.py, which override this fixture's monkeypatches.
"""

import pytest


@pytest.fixture(autouse=True)
def _bind_test_identity(monkeypatch, request):
    tenant_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("AZURE_TENANT_ID", tenant_id)
    if request.module.__name__.rsplit(".", 1)[-1] in {"test_auth", "test_entra_tokens"}:
        return
    from services import auth
    from services.entra_tokens import TokenIdentity

    monkeypatch.setattr(auth, "resolve_user_token", lambda request, expected_tenant_id: TokenIdentity(
        tenant_id=tenant_id, object_id="22222222-2222-2222-2222-222222222222",
        subject="user-1", display_name="user@example.test",
    ))


@pytest.fixture(autouse=True)
def _authorize_all_subscriptions_by_default(monkeypatch):
    import main

    async def allow_all(principal_object_id, subscription_ids):
        return list(subscription_ids)

    async def allow_one(principal_object_id, subscription_id):
        return None

    async def allow_bundle(principal_object_id, subscription_ids):
        return None

    async def allow_operation(principal_object_id, subscription_id, operation):
        return None

    monkeypatch.setattr(main.access_control, "authorized_subscription_ids", allow_all)
    monkeypatch.setattr(main.access_control, "require_subscription_access", allow_one)
    monkeypatch.setattr(main.access_control, "require_all_subscription_access", allow_bundle)
    monkeypatch.setattr(main.access_control, "require_subscription_operation", allow_operation)
