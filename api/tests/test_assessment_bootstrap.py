import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from jobs import assessment_bootstrap

API_PRINCIPAL = "11111111-1111-1111-1111-111111111111"
WORKER_PRINCIPAL = "22222222-2222-2222-2222-222222222222"


def principal(role, object_id, *, principal_type="service", admin=0):
    return {
        "rolname": role,
        "principaltype": principal_type,
        "objectid": object_id,
        "tenantid": "33333333-3333-3333-3333-333333333333",
        "ismfa": 0,
        "isadmin": admin,
    }


@pytest.fixture
def bootstrap_database(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_API_PRINCIPAL_ID", API_PRINCIPAL)
    monkeypatch.setenv("ASSESSMENT_WORKER_PRINCIPAL_ID", WORKER_PRINCIPAL)
    monkeypatch.setenv("ASSESSMENT_DB_NAME", "postgres")
    monkeypatch.setenv("ASSESSMENT_TARGET_DB_NAME", "meghkosha")
    connection = MagicMock()
    connection.execute = AsyncMock()
    database = MagicMock()
    database.fetch = AsyncMock(return_value=[
        principal("id-db-bootstrap", "44444444-4444-4444-4444-444444444444", admin=1),
    ])
    database.execute = AsyncMock()
    database.acquire.return_value.__aenter__.return_value = connection
    close = AsyncMock()
    monkeypatch.setattr(assessment_bootstrap.assessment_store, "pool", AsyncMock(return_value=database))
    monkeypatch.setattr(assessment_bootstrap.assessment_store, "close", close)
    return database, connection, close


def test_bootstrap_creates_runtime_roles_from_azure_principal_contract(bootstrap_database):
    database, connection, close = bootstrap_database
    asyncio.run(assessment_bootstrap.run())
    query = "SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid($1,$2,'service',false,false)"
    assert database.execute.await_args_list == [
        call(query, "meghkosha_api", API_PRINCIPAL),
        call(query, "meghkosha_worker", WORKER_PRINCIPAL),
    ]
    assert connection.execute.await_count == 2
    assert "CREATE SCHEMA IF NOT EXISTS assessments" in connection.execute.await_args_list[0].args[0]
    assert connection.execute.await_args_list[1].args == (assessment_bootstrap.RUNTIME_GRANTS,)
    assert close.await_count == 2


def test_bootstrap_reuses_matching_unprivileged_runtime_principals(bootstrap_database):
    database, connection, close = bootstrap_database
    database.fetch.return_value = [
        principal("meghkosha_api", API_PRINCIPAL),
        principal("meghkosha_worker", WORKER_PRINCIPAL),
    ]
    asyncio.run(assessment_bootstrap.run())
    database.execute.assert_not_awaited()
    assert connection.execute.await_count == 2
    assert close.await_count == 2


@pytest.mark.parametrize("changes", [
    {"objectid": WORKER_PRINCIPAL},
    {"principaltype": "user"},
    {"isadmin": 1},
])
def test_bootstrap_rejects_mismatched_or_elevated_existing_roles(bootstrap_database, changes):
    database, connection, close = bootstrap_database
    database.fetch.return_value = [{**principal("meghkosha_api", API_PRINCIPAL), **changes}]
    with pytest.raises(RuntimeError, match="different identity or elevated privileges"):
        asyncio.run(assessment_bootstrap.run())
    database.execute.assert_not_awaited()
    connection.execute.assert_not_awaited()
    close.assert_awaited_once()
