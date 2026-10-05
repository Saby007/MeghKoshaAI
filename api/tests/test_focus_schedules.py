import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from fastapi import HTTPException

from services import focus_schedules

TENANT = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACTOR = "22222222-2222-2222-2222-222222222222"
STORAGE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/teststore"
SUB = {"subscriptionId": SUBSCRIPTION, "displayName": "Verified subscription", "readAccess": True, "costAccess": True}


@pytest.fixture
def store(monkeypatch):
    documents, leases, requests, native_runs, daily_runs = {}, set(), [], [], []
    monkeypatch.setenv("APP_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_CLIENT_ID", ACTOR)
    monkeypatch.setenv("COST_EXPORT_STORAGE_URL", "https://teststore.blob.core.windows.net")
    monkeypatch.setenv("COST_EXPORT_STORAGE_RESOURCE_ID", STORAGE)
    monkeypatch.setenv("COST_EXPORT_CONTAINER", "cost-exports")

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_blob(self, **kwargs):
            if self.name not in documents:
                raise ResourceNotFoundError("Missing")
            return SimpleNamespace(readall=lambda: documents[self.name])

        def upload_blob(self, value, overwrite=False, lease=None, **kwargs):
            if self.name in documents and not overwrite:
                raise ResourceExistsError("Exists")
            if overwrite:
                assert self.name in leases and lease is not None
            documents[self.name] = value

        def acquire_lease(self, lease_duration):
            assert lease_duration == 60
            if self.name not in documents:
                raise ResourceNotFoundError("Missing")
            if self.name in leases:
                raise ResourceExistsError("Busy", status_code=409)
            leases.add(self.name)
            return SimpleNamespace(release=lambda: leases.remove(self.name))

    @contextmanager
    def container():
        yield SimpleNamespace(get_blob_client=Blob, list_blobs=lambda **kwargs: [SimpleNamespace(name=name) for name in documents])

    async def native(method, subscription_id, suffix="", body=None, kind="monthly"):
        requests.append((method, subscription_id, suffix, body, kind))
        assert subscription_id == SUBSCRIPTION
        if suffix == "permissions":
            return {"value": [{"actions": ["Microsoft.CostManagement/*"], "notActions": []}]}
        if suffix == "runHistory":
            return {"value": list(native_runs if kind == "monthly" else daily_runs)}
        if suffix == "run":
            return {}
        return {"identity": {"type": "SystemAssigned", "tenantId": TENANT, "principalId": ACTOR}, "properties": {
            "definition": {"type": "FocusCost", "timeframe": "Custom", "dataSet": {"configuration": {"dataVersion": "1.2-preview"}}},
            "format": "Csv", "compressionMode": "gzip", "schedule": {"status": "Inactive"},
            "dataOverwriteBehavior": "OverwritePreviousReport", "partitionData": True,
            "deliveryInfo": {"destination": {"resourceId": STORAGE, "container": "cost-exports",
                                             "rootFolderPath": focus_schedules.focus_export_control.root_folder(SUBSCRIPTION, kind)}},
        }}

    monkeypatch.setattr(focus_schedules, "_container", container)
    monkeypatch.setattr(focus_schedules, "_native_request", native)
    return SimpleNamespace(documents=documents, leases=leases, requests=requests, native_runs=native_runs, daily_runs=daily_runs)


@pytest.mark.parametrize("now, first, last", [
    ("2026-09-13", "2026-03-01", "2026-08-31"),
    ("2026-01-01", "2025-07-01", "2025-12-31"),
    ("2024-03-01", "2023-09-01", "2024-02-29"),
])
def test_six_completed_calendar_months(now, first, last):
    months = focus_schedules.completed_months(datetime.fromisoformat(now).replace(tzinfo=timezone.utc))
    assert len(months) == 6
    assert str(months[0].start.date()) == first
    assert str(months[-1].end.date()) == last
    assert all(after.start == before.end + timedelta(seconds=1) for before, after in zip(months, months[1:]))


@pytest.mark.parametrize("method,timeout", [("GET", 10), ("POST", 10), ("PUT", 45)])
def test_export_control_timeout_remains_bounded_by_operation(store, monkeypatch, method, timeout):
    actual_client = httpx.AsyncClient
    requests = []

    async def token(scope):
        assert scope == "https://management.azure.com/.default"
        return SimpleNamespace(token="synthetic-managed-identity-token")

    @asynccontextmanager
    async def credential(*, client_id):
        assert client_id == ACTOR
        yield SimpleNamespace(get_token=token)

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == timeout
        assert request.headers["Authorization"] == "Bearer synthetic-managed-identity-token"
        assert request.headers["If-None-Match"] == "*"
        return httpx.Response(200, json={"accepted": True})

    def client(**options):
        assert options["follow_redirects"] is False
        return actual_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(focus_schedules, "AsyncManagedIdentityCredential", credential)
    monkeypatch.setattr(focus_schedules.httpx, "AsyncClient", client)
    result = asyncio.run(focus_schedules._arm_request(method, "/test", headers={"If-None-Match": "*"}))
    assert result == {"accepted": True} and len(requests) == 1


def test_monthly_schedule_keeps_original_day_after_february():
    start = datetime(2024, 1, 31, 3, tzinfo=timezone.utc)
    assert focus_schedules.next_monthly_run(start, datetime(2024, 2, 29, 4, tzinfo=timezone.utc)) == datetime(2024, 3, 31, 3, tzinfo=timezone.utc)


def test_create_saves_six_month_schedule_without_creating_export_or_grants(store):
    result = asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z"))
    assert result["windowMonths"] == 6 and result["state"] == "active"
    assert all(method == "GET" for method, *_ in store.requests)
    raw = next(iter(store.documents.values()))
    assert b'"window_months":6' in raw and b"assertion" not in raw
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z"))
    assert error.value.status_code == 409


def test_configure_existing_export_does_not_rewrite_run_or_schedule(store):
    result = asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert result["subscriptionId"] == SUBSCRIPTION
    assert result["state"] == "configured" and result["created"] is False
    assert all(method == "GET" for method, *_ in store.requests)
    assert not store.documents


@pytest.fixture
def export_setup(store, monkeypatch):
    original = focus_schedules._native_request
    setup = SimpleNamespace(created=set(), calls=[], permissions={"value": [{"actions": ["*"], "notActions": []}]},
                            storage={"properties": {"isHnsEnabled": True, "allowSharedKeyAccess": False,
                                                     "allowBlobPublicAccess": False, "publicNetworkAccess": "Enabled",
                                                     "networkAcls": {"defaultAction": "Deny", "bypass": "AzureServices"}}},
                            public_access="None", put_error=None, readback_format="Csv")

    async def native(method, subscription_id, suffix="", body=None, kind="monthly"):
        if method == "GET" and not suffix and kind not in setup.created:
            raise httpx.HTTPStatusError("Missing", request=httpx.Request("GET", "https://management.azure.com"), response=httpx.Response(404))
        result = await original(method, subscription_id, suffix, body, kind)
        if "properties" in result:
            result["properties"]["format"] = setup.readback_format
        return result

    async def arm(method, path, body=None, **kwargs):
        setup.calls.append((method, path, body, kwargs))
        if method == "PUT":
            kind = next(kind for kind in ("monthly", "daily") if path == focus_schedules.focus_export_control._export_path(SUBSCRIPTION, kind))
            assert kwargs == {"headers": {"If-None-Match": "*"}}
            setup.created.add(kind)
            if setup.put_error:
                raise setup.put_error
            return await original("GET", SUBSCRIPTION, kind=kind)
        assert method == "GET"
        if "/permissions?" in path:
            return setup.permissions
        if "/containers/" in path:
            return {"properties": {"publicAccess": setup.public_access}}
        assert path.startswith(STORAGE + "?")
        return setup.storage

    monkeypatch.setattr(focus_schedules, "_native_request", native)
    monkeypatch.setattr(focus_schedules, "_arm_request", arm)
    return setup


@pytest.mark.parametrize("public_network_access", ["Enabled", "Disabled"])
def test_export_setup_preview_never_creates_or_schedules(store, export_setup, public_network_access):
    export_setup.storage["properties"]["publicNetworkAccess"] = public_network_access
    result = asyncio.run(focus_schedules.export_configuration(SUB))
    assert result["state"] == "missing" and result["canConfigure"] is True
    assert result["destinationRoleScope"] == f"{STORAGE}/blobServices/default/containers/cost-exports"
    assert result["rootFolderPath"] == f"focus/{SUBSCRIPTION}"
    assert result["nativeSchedule"] == "Inactive" and result["windowMonths"] == 6
    assert result["daily"] == {"exportName": "focus-daily-meghkoshaai", "rootFolderPath": f"focus-daily/{SUBSCRIPTION}",
                               "state": "missing", "canConfigure": True}
    assert all(method == "GET" for method, *_ in export_setup.calls)
    assert not export_setup.created and not store.documents


@pytest.mark.parametrize("public_network_access", ["Enabled", "Disabled"])
def test_export_setup_creates_both_inactive_focus_exports_then_reuses_them(store, export_setup, public_network_access):
    export_setup.storage["properties"]["publicNetworkAccess"] = public_network_access
    result = asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert result["created"] is True and result["state"] == "configured" and result["daily"]["state"] == "configured"
    mutations = [call for call in export_setup.calls if call[0] != "GET"]
    assert [call[1] for call in mutations] == [focus_schedules.focus_export_control._export_path(SUBSCRIPTION, kind) for kind in ("monthly", "daily")]
    for (_, _, body, _), root, timeframe, recurrence in zip(
            mutations, (f"focus/{SUBSCRIPTION}", f"focus-daily/{SUBSCRIPTION}"), ("TheLastMonth", "MonthToDate"), ("Monthly", "Daily")):
        assert body["identity"] == {"type": "SystemAssigned"}
        assert body["properties"]["definition"]["type"] == "FocusCost"
        assert body["properties"]["definition"]["timeframe"] == timeframe
        assert body["properties"]["schedule"]["status"] == "Inactive"
        assert body["properties"]["schedule"]["recurrence"] == recurrence
        assert body["properties"]["dataOverwriteBehavior"] == "OverwritePreviousReport"
        assert body["properties"]["partitionData"] is True
        assert body["properties"]["deliveryInfo"]["destination"]["resourceId"] == STORAGE
        assert body["properties"]["deliveryInfo"]["destination"]["rootFolderPath"] == root
    assert not store.documents
    result = asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert result["created"] is False
    assert len([call for call in export_setup.calls if call[0] != "GET"]) == 2
    assert not store.documents and all(call[0] == "GET" for call in store.requests)


def test_export_setup_adds_only_the_missing_daily_export(store, export_setup):
    export_setup.created.add("monthly")
    preview = asyncio.run(focus_schedules.export_configuration(SUB))
    assert preview["state"] == "configured" and preview["canConfigure"] is False
    assert preview["daily"]["state"] == "missing" and preview["daily"]["canConfigure"] is True
    result = asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert result["created"] is True and result["daily"]["state"] == "configured"
    assert [call[1] for call in export_setup.calls if call[0] == "PUT"] == [
        focus_schedules.focus_export_control._export_path(SUBSCRIPTION, "daily")]


@pytest.mark.parametrize("acknowledged,access,status", [(False, True, 400), (True, False, 403)])
def test_export_setup_requires_confirmation_and_verified_access_before_azure(store, export_setup, acknowledged, access, status):
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export({**SUB, "costAccess": access}, allow_destination_role_assignment=acknowledged))
    assert error.value.status_code == status
    assert not export_setup.calls and not store.documents and not store.requests


@pytest.mark.parametrize("permission", [
    {"actions": ["*/read"], "notActions": []},
    {"actions": ["*"], "notActions": ["Microsoft.CostManagement/exports/write"]},
    {"actions": ["*"], "notActions": [], "condition": "unknown", "conditionVersion": "2.0"},
    {"actions": ["*"], "notActions": ["Microsoft.Authorization/roleAssignments/write"]},
])
def test_export_setup_requires_existing_unconditional_permissions(store, export_setup, permission):
    export_setup.permissions = {"value": [permission]}
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == 403
    assert not export_setup.created and all(call[0] == "GET" for call in export_setup.calls)


@pytest.mark.parametrize("change", [
    {"publicNetworkAccess": "SecuredByPerimeter"}, {"publicNetworkAccess": None}, {"publicNetworkAccess": "Unknown"},
    {"publicNetworkAccess": "Disabled", "networkAcls": {"defaultAction": "Deny", "bypass": "None"}},
    {"allowSharedKeyAccess": True}, {"allowBlobPublicAccess": True},
    {"networkAcls": {"defaultAction": "Allow", "bypass": "AzureServices"}},
    {"networkAcls": {"defaultAction": "Deny", "bypass": "None"}}, {"allowedCopyScope": "AAD"},
])
def test_export_setup_never_changes_storage_networking_or_security(store, export_setup, change):
    export_setup.storage["properties"].update(change)
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == 409
    assert "will not be changed" in error.value.detail
    assert not export_setup.created and all(call[0] == "GET" for call in export_setup.calls)


def test_mismatched_export_readback_does_not_claim_success_or_save_schedule(store, export_setup):
    export_setup.readback_format = "Parquet"
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == 409
    assert export_setup.created and len([call for call in export_setup.calls if call[0] == "PUT"]) == 2
    assert not store.documents


def test_uncertain_export_creation_is_not_retried_or_scheduled(store, export_setup):
    export_setup.put_error = httpx.ReadTimeout("private upstream detail")
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == 503 and "private" not in error.value.detail
    assert len([call for call in export_setup.calls if call[0] == "PUT"]) == 1
    assert not store.documents
    # The uncertain closed-month export is found rather than replayed; only the missing daily one is created.
    export_setup.put_error = None
    assert asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))["created"] is True
    assert [call[1] for call in export_setup.calls if call[0] == "PUT"] == [
        focus_schedules.focus_export_control._export_path(SUBSCRIPTION, kind) for kind in ("monthly", "daily")]


@pytest.mark.parametrize("status,expected", [(403, 403), (409, 409), (412, 409), (429, 503), (500, 503)])
def test_export_setup_handles_native_failures_without_replay(store, export_setup, status, expected):
    export_setup.put_error = httpx.HTTPStatusError("private Azure response", request=httpx.Request("PUT", "https://management.azure.com"),
                                                  response=httpx.Response(status, headers={"Retry-After": "120"}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == expected and "private" not in error.value.detail
    if status == 429:
        assert "FOCUS export configuration was throttled" in error.value.detail
        assert error.value.headers["Retry-After"] == "120"
    assert not store.documents and len([call for call in export_setup.calls if call[0] == "PUT"]) == 1


def test_load_never_creates_or_writes_anything_when_export_is_missing(store, export_setup):
    result = asyncio.run(focus_schedules.load(SUB))
    assert result["availability"] == "export_unavailable"
    assert not export_setup.created
    assert len([call for call in export_setup.calls if call[0] == "PUT"]) == 0
    assert not store.documents
    # A second poll must not attempt to fix it either; only an explicit Export action may.
    second = asyncio.run(focus_schedules.load(SUB))
    assert second["availability"] == "export_unavailable"
    assert len([call for call in export_setup.calls if call[0] == "PUT"]) == 0
    assert not store.documents


def test_load_surfaces_missing_write_permission_without_ever_retrying(store, export_setup):
    export_setup.permissions = {"value": [{"actions": ["*/read"], "notActions": []}]}
    first = asyncio.run(focus_schedules.load(SUB))
    assert first["availability"] == "export_unavailable"
    assert not export_setup.created and not store.documents
    calls_after_first = len(export_setup.calls)
    second = asyncio.run(focus_schedules.load(SUB))
    assert second["availability"] == "export_unavailable"
    assert len(export_setup.calls) == calls_after_first
    assert not store.documents
    # The specific permission diagnostic is still available through the explicit Export action.
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.export_configuration(SUB))
    assert "pre-granted FOCUS export write permission" in error.value.detail


@pytest.mark.parametrize("change", [{"schedule": {"status": "Active"}}, {"format": "Parquet"},
                                    {"compressionMode": "None"}, {"definition": {"type": "FocusCost", "dataSet": {}}}])
def test_export_setup_never_overwrites_an_incompatible_export(store, monkeypatch, change):
    original = focus_schedules._native_request

    async def incompatible(*args, **kwargs):
        result = await original(*args, **kwargs)
        if "properties" in result:
            result["properties"].update(change)
        return result

    monkeypatch.setattr(focus_schedules, "_native_request", incompatible)
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.configure_export(SUB, allow_destination_role_assignment=True))
    assert error.value.status_code == 409
    assert all(call[0] == "GET" for call in store.requests) and not store.documents


def test_missing_worker_blocks_schedule_before_writes(store, monkeypatch):
    monkeypatch.delenv("APP_SCHEDULER_ENABLED")
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z"))
    assert error.value.status_code == 503 and not store.documents and not store.requests


def test_schedule_listing_reports_missing_worker_without_hiding_subscription(store, monkeypatch):
    monkeypatch.delenv("APP_SCHEDULER_ENABLED")
    row = asyncio.run(focus_schedules.load(SUB))
    assert row["subscriptionId"] == SUBSCRIPTION
    assert row["state"] == "unknown" and row["availability"] == "configuration_unavailable"
    assert "worker is not enabled" in row["statusMessage"]
    assert row["readAccess"] and row["costAccess"]
    assert not store.requests and not store.documents


def test_unverified_runtime_access_does_not_load_schedule_or_overstate_permissions(store):
    row = asyncio.run(focus_schedules.load({**SUB, "readAccess": False, "costAccess": False,
                                           "accessIssue": "Azure subscription access check was throttled."}))
    assert row["state"] == "unknown" and row["availability"] == "access_unavailable"
    assert not row["readAccess"] and not row["costAccess"]
    assert row["statusMessage"] == "Azure subscription access check was throttled."
    assert row["latestRun"] is None and row["nextRunAt"] is None
    assert not store.requests and not store.documents


def test_schedule_listing_reports_missing_adls_without_reading_native_exports(store, monkeypatch):
    monkeypatch.delenv("COST_EXPORT_STORAGE_URL")
    row = asyncio.run(focus_schedules.load(SUB))
    assert row["availability"] == "configuration_unavailable"
    assert "ADLS configuration" in row["statusMessage"]
    assert not store.requests and not store.documents


def test_missing_native_export_never_creates_one(store, monkeypatch):
    async def missing(*args, **kwargs):
        raise httpx.HTTPStatusError("missing", request=httpx.Request("GET", "https://management.azure.com"), response=httpx.Response(404))

    monkeypatch.setattr(focus_schedules, "_native_request", missing)
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z"))
    assert error.value.status_code == 503 and not store.documents


def test_read_only_worker_cannot_schedule_export_execution(store, monkeypatch):
    original = focus_schedules._native_request

    async def read_only(method, subscription_id, suffix="", body=None, kind="monthly"):
        if suffix == "permissions":
            return {"value": [{"actions": ["*/read"], "notActions": []}]}
        return await original(method, subscription_id, suffix, body, kind)

    monkeypatch.setattr(focus_schedules, "_native_request", read_only)
    with pytest.raises(HTTPException) as error:
        asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z"))
    assert error.value.status_code == 503 and not store.documents


def test_each_cycle_submits_the_next_month_in_the_tick_that_completes_the_previous(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=now))["status"] == "queued"
        for index in range(6):
            submitted = [call for call in store.requests if call[0] == "POST"]
            assert len(submitted) == index + 1
            await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=1))
            assert len([call for call in store.requests if call[0] == "POST"]) == index + 1
            period = submitted[-1][3]["timePeriod"]
            store.native_runs.append({"name": f"run-{index}", "properties": {
                "status": "Completed", "startDate": period["from"], "endDate": period["to"], "submittedTime": now.isoformat(),
            }})
            result = await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=2))
            assert result["status"] == ("succeeded" if index == 5 else "queued")
            now += timedelta(hours=1)
        assert len([call for call in store.requests if call[0] == "POST"]) == 6
        view = await focus_schedules.load(SUB)
        assert view["latestRun"]["status"] == "succeeded"
        assert view["latestRun"]["completedMonths"] == 6
        assert view["latestRun"]["months"] == [
            {"period": period, "status": "succeeded"}
            for period in ("2030-03", "2030-04", "2030-05", "2030-06", "2030-07", "2030-08")
        ]
        assert view["nextRunAt"] == "2030-10-05T03:00:00Z"
        assert (await focus_schedules.advance(SUBSCRIPTION, now=now))["status"] == "not_due"
        await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 10, 5, 4, tzinfo=timezone.utc))
        submitted = [call for call in store.requests if call[0] == "POST"]
        assert len(submitted) == 7
        # The scheduled close only pulls the newly closed month; Export pulled the rest after Azure finalized them.
        assert submitted[-1][3]["timePeriod"]["from"] == "2030-09-01T00:00:00Z"
        latest = (await focus_schedules.load(SUB))["latestRun"]
        assert latest["kind"] == "close" and latest["period"] == "2030-09" and latest["windowMonths"] == 6
        assert all(call[0] == "GET" or call[2] == "run" for call in store.requests)

    asyncio.run(exercise())
    assert not store.leases


@pytest.mark.parametrize("native_status", ["Queued", "InProgress", "DataReady"])
def test_intermediate_native_states_do_not_complete_or_resubmit_a_month(store, native_status):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        period = next(call[3]["timePeriod"] for call in store.requests if call[0] == "POST")
        store.native_runs.append({"name": "pending-delivery", "properties": {
            "status": native_status, "startDate": period["from"], "endDate": period["to"],
            "submittedTime": now.isoformat(),
        }})
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=1))
        view = await focus_schedules.load(SUB)
        assert view["latestRun"]["status"] == "running" and view["latestRun"]["completedMonths"] == 0
        assert len([call for call in store.requests if call[0] == "POST"]) == 1

    asyncio.run(exercise())


def test_pause_and_delete_only_change_metadata(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        await focus_schedules.update(SUB, ACTOR, "paused")
        assert (await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 9, 6, tzinfo=timezone.utc)))["status"] == "not_due"
        await focus_schedules.update(SUB, ACTOR, "deleted")
        assert (await focus_schedules.load(SUB))["state"] == "not_scheduled"

    asyncio.run(exercise())
    assert all(call[0] == "GET" for call in store.requests)
    assert b'"state":"deleted"' in next(iter(store.documents.values()))


def test_deleted_schedule_can_be_onboarded_again_without_native_changes(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        await focus_schedules.update(SUB, ACTOR, "deleted")
        result = await focus_schedules.create(SUB, ACTOR, "2030-10-05T04:00:00Z")
        assert result["state"] == "active" and result["nextRunAt"] == "2030-10-05T04:00:00Z"

    asyncio.run(exercise())
    assert len(store.documents) == 1 and all(call[0] == "GET" for call in store.requests)


def test_busy_lease_blocks_a_second_writer(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        async with focus_schedules._locked(SUBSCRIPTION):
            with pytest.raises(HTTPException) as error:
                await focus_schedules.update(SUB, ACTOR, "paused")
            assert error.value.status_code == 409

    asyncio.run(exercise())
    assert not store.leases


def test_ambiguous_submission_is_reconciled_without_resubmission(store, monkeypatch):
    original = focus_schedules._native_request

    async def uncertain(method, subscription_id, suffix="", body=None, kind="monthly"):
        result = await original(method, subscription_id, suffix, body, kind)
        if suffix == "run":
            raise httpx.ReadTimeout("Response lost")
        return result

    monkeypatch.setattr(focus_schedules, "_native_request", uncertain)

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=15))
        assert len([call for call in store.requests if call[0] == "POST"]) == 1
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(hours=25))
        assert (await focus_schedules.load(SUB))["latestRun"]["status"] == "failed"

    asyncio.run(exercise())


def test_worker_uses_only_stored_scopes_and_does_not_overlap_them(store):
    from jobs import scheduler

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_enabled=False)
        assert await scheduler.run_once() == 0

    asyncio.run(exercise())
    assert not [call for call in store.requests if call[0] == "POST"]


def test_active_scheduled_subscription_ids_excludes_paused_and_deleted(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        assert await focus_schedules.active_scheduled_subscription_ids() == [SUBSCRIPTION]
        await focus_schedules.update(SUB, ACTOR, "paused")
        assert await focus_schedules.active_scheduled_subscription_ids() == []
        await focus_schedules.update(SUB, ACTOR, "active", "2030-10-05T03:00:00Z")
        assert await focus_schedules.active_scheduled_subscription_ids() == [SUBSCRIPTION]
        await focus_schedules.update(SUB, ACTOR, "deleted")
        assert await focus_schedules.active_scheduled_subscription_ids() == []

    asyncio.run(exercise())


async def not_due(subscription_id, **kwargs):
    return {"subscriptionId": subscription_id, "status": "not_due"}


def test_scheduler_refreshes_report_for_active_schedules_after_a_completed_cycle(store, monkeypatch):
    from jobs import scheduler
    import main

    calls = []

    async def fake_advance(subscription_id, **kwargs):
        return {"subscriptionId": subscription_id, "status": "succeeded"}

    async def fake_active_ids():
        return [SUBSCRIPTION]

    async def fake_build_and_publish(subscription_ids, stale_days):
        calls.append((subscription_ids, stale_days))

    monkeypatch.setattr(focus_schedules, "scheduled_subscription_ids", lambda: [SUBSCRIPTION])
    monkeypatch.setattr(focus_schedules, "advance", fake_advance)
    monkeypatch.setattr(focus_schedules, "advance_daily", not_due)
    monkeypatch.setattr(focus_schedules, "active_scheduled_subscription_ids", fake_active_ids)
    monkeypatch.setattr(main, "_build_and_publish_report", fake_build_and_publish)

    failures = asyncio.run(scheduler.run_once())
    assert failures == 0
    assert calls == [([SUBSCRIPTION], 90)]


def test_scheduler_does_not_refresh_report_when_no_cycle_completes(store, monkeypatch):
    from jobs import scheduler
    import main

    calls = []

    async def fake_advance(subscription_id, **kwargs):
        return {"subscriptionId": subscription_id, "status": "not_due"}

    async def fake_build_and_publish(subscription_ids, stale_days):
        calls.append((subscription_ids, stale_days))

    monkeypatch.setattr(focus_schedules, "scheduled_subscription_ids", lambda: [SUBSCRIPTION])
    monkeypatch.setattr(focus_schedules, "advance", fake_advance)
    monkeypatch.setattr(focus_schedules, "advance_daily", not_due)
    monkeypatch.setattr(main, "_build_and_publish_report", fake_build_and_publish)

    failures = asyncio.run(scheduler.run_once())
    assert failures == 0
    assert calls == []


def test_scheduler_report_refresh_failure_does_not_affect_scheduler_failure_count(store, monkeypatch):
    from jobs import scheduler
    import main

    async def fake_advance(subscription_id, **kwargs):
        return {"subscriptionId": subscription_id, "status": "succeeded"}

    async def fake_active_ids():
        return [SUBSCRIPTION]

    async def failing_build(subscription_ids, stale_days):
        raise RuntimeError("boom")

    monkeypatch.setattr(focus_schedules, "scheduled_subscription_ids", lambda: [SUBSCRIPTION])
    monkeypatch.setattr(focus_schedules, "advance", fake_advance)
    monkeypatch.setattr(focus_schedules, "advance_daily", not_due)
    monkeypatch.setattr(focus_schedules, "active_scheduled_subscription_ids", fake_active_ids)
    monkeypatch.setattr(main, "_build_and_publish_report", failing_build)

    failures = asyncio.run(scheduler.run_once())
    assert failures == 0


def test_unavailable_month_never_completes_the_six_month_window(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        period = next(call[3]["timePeriod"] for call in store.requests if call[0] == "POST")
        store.native_runs.append({"name": "missing-month", "properties": {
            "status": "DataNotAvailable", "startDate": period["from"], "endDate": period["to"],
        }})
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=15))
        result = await focus_schedules.load(SUB)
        assert result["latestRun"]["status"] == "failed"
        assert result["latestRun"]["completedMonths"] == 0
        assert result["nextRunAt"] == "2030-10-05T03:00:00Z"

    asyncio.run(exercise())


def test_throttled_month_honors_retry_after_before_resubmitting(store, monkeypatch):
    original = focus_schedules._native_request
    submissions = 0

    async def throttled(method, subscription_id, suffix="", body=None, kind="monthly"):
        nonlocal submissions
        result = await original(method, subscription_id, suffix, body, kind)
        if suffix == "run":
            submissions += 1
            if submissions == 1:
                request = httpx.Request("POST", "https://management.azure.com/test")
                raise httpx.HTTPStatusError("throttled", request=request, response=httpx.Response(429, headers={"Retry-After": "600"}, request=request))
        return result

    monkeypatch.setattr(focus_schedules, "_native_request", throttled)

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=1))
        assert submissions == 1
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(seconds=601))
        assert submissions == 2

    asyncio.run(exercise())


def test_pausing_an_active_cycle_stops_additional_month_submissions(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        period = next(call[3]["timePeriod"] for call in store.requests if call[0] == "POST")
        store.native_runs.append({"name": "first-month", "properties": {
            "status": "Completed", "startDate": period["from"], "endDate": period["to"],
        }})
        await focus_schedules.update(SUB, ACTOR, "paused")
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=15))
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=30))
        assert len([call for call in store.requests if call[0] == "POST"]) == 1
        await focus_schedules.update(SUB, ACTOR, "active")
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=45))
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=60))
        assert len([call for call in store.requests if call[0] == "POST"]) == 2

    asyncio.run(exercise())


def test_completed_month_discards_temporary_history_baseline(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        store.native_runs.append({"name": "previous-run", "properties": {"status": "Completed"}})
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=now)
        period = next(call[3]["timePeriod"] for call in store.requests if call[0] == "POST")
        store.native_runs.append({"name": "current-month", "properties": {
            "status": "Completed", "startDate": period["from"], "endDate": period["to"],
        }})
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=15))
        stored = focus_schedules.StoredSchedule.model_validate_json(next(iter(store.documents.values())))
        assert stored.cycle.months[0].status == "succeeded"
        assert stored.cycle.months[0].baseline_ids == []
        assert stored.cycle.months[0].run_id == "current-month"

    asyncio.run(exercise())


@pytest.mark.parametrize("window, first", [(3, "2026-06-01"), (4, "2026-05-01"), (5, "2026-04-01"), (6, "2026-03-01")])
def test_completed_months_follow_the_selected_window(window, first):
    months = focus_schedules.completed_months(datetime(2026, 9, 13, tzinfo=timezone.utc), window)
    assert len(months) == window
    assert str(months[0].start.date()) == first and str(months[-1].end.date()) == "2026-08-31"


@pytest.mark.parametrize("window", [0, 1, 2, 7, 12, True])
def test_windows_outside_three_to_six_months_are_rejected_before_any_write(store, window):
    with pytest.raises(ValueError):
        focus_schedules.completed_months(datetime(2026, 9, 13, tzinfo=timezone.utc), window)
    with pytest.raises(ValueError):
        asyncio.run(focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", window))
    assert not store.documents and not store.requests


def test_selected_window_is_stored_and_used_from_the_next_cycle(store):
    async def exercise():
        created = await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", 4)
        assert created["windowMonths"] == 4
        assert b'"window_months":4' in next(iter(store.documents.values()))
        await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 9, 5, 4, tzinfo=timezone.utc))
        view = await focus_schedules.load(SUB)
        assert view["latestRun"]["windowMonths"] == 4
        assert [month["period"] for month in view["latestRun"]["months"]] == ["2030-05", "2030-06", "2030-07", "2030-08"]
        assert next(call[3]["timePeriod"]["from"] for call in store.requests if call[0] == "POST") == "2030-05-01T00:00:00Z"
        updated = await focus_schedules.update(SUB, ACTOR, "active", window_months=3)
        assert updated["windowMonths"] == 3 and updated["latestRun"]["windowMonths"] == 4

    asyncio.run(exercise())


@pytest.mark.parametrize("value, expected", [(None, 1), ("abc", 1), ("0", 1), ("4", 4), ("99", 6)])
def test_parallel_month_setting_is_bounded(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("FOCUS_EXPORT_PARALLEL_MONTHS", raising=False)
    else:
        monkeypatch.setenv("FOCUS_EXPORT_PARALLEL_MONTHS", value)
    assert focus_schedules._parallel_months() == expected


def test_parallel_setting_submits_several_months_and_counts_outside_runs(store, monkeypatch):
    monkeypatch.setenv("FOCUS_EXPORT_PARALLEL_MONTHS", "3")

    def posts():
        return [call[3]["timePeriod"] for call in store.requests if call[0] == "POST"]

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=now))["status"] == "queued"
        assert [period["from"] for period in posts()] == ["2030-03-01T00:00:00Z", "2030-04-01T00:00:00Z", "2030-05-01T00:00:00Z"]
        for name, period, status in zip(("m0", "m1", "m2"), posts(), ("Completed", "InProgress", "Queued")):
            store.native_runs.append({"name": name, "properties": {"status": status, "startDate": period["from"], "endDate": period["to"]}})
        # A run started outside the cycle, for example from the portal, takes the freed slot.
        store.native_runs.append({"name": "portal", "properties": {"status": "InProgress"}})
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=5))
        assert len(posts()) == 3
        store.native_runs[-1]["properties"]["status"] = "Completed"
        await focus_schedules.advance(SUBSCRIPTION, now=now + timedelta(minutes=10))
        assert [period["from"] for period in posts()[3:]] == ["2030-06-01T00:00:00Z"]

    asyncio.run(exercise())


def _throttled(headers):
    request = httpx.Request("POST", "https://management.azure.com/test")
    return httpx.HTTPStatusError("throttled", request=request, response=httpx.Response(429, headers=headers, request=request))


@pytest.mark.parametrize("headers, seconds", [
    ({"x-ms-ratelimit-microsoft.consumption-retry-after": "120", "Retry-After": "600"}, 120),
    ({"Retry-After": "30"}, 60),
    ({"x-ms-ratelimit-microsoft.consumption-retry-after": "86400"}, 900),
    ({"Retry-After": "Wed, 21 Oct 2030 07:28:00 GMT"}, 900),
    ({}, 900),
])
def test_throttle_wait_follows_azure_within_one_to_fifteen_minutes(headers, seconds):
    assert focus_schedules._throttle_delay(httpx.Headers(headers)) == timedelta(seconds=seconds)


def test_repeated_throttling_keeps_attempts_and_holds_every_month(store, monkeypatch):
    original = focus_schedules._native_request
    remaining = 5
    submissions = []

    async def throttled(method, subscription_id, suffix="", body=None, kind="monthly"):
        nonlocal remaining
        result = await original(method, subscription_id, suffix, body, kind)
        if suffix == "run":
            submissions.append(body["timePeriod"]["from"])
            if remaining:
                remaining -= 1
                raise _throttled({"x-ms-ratelimit-microsoft.consumption-retry-after": "120"})
        return result

    monkeypatch.setattr(focus_schedules, "_native_request", throttled)
    monkeypatch.setenv("FOCUS_EXPORT_PARALLEL_MONTHS", "3")

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        start = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=start)
        await focus_schedules.advance(SUBSCRIPTION, now=start + timedelta(minutes=1))
        assert submissions == ["2030-03-01T00:00:00Z"]
        for tick in range(1, 6):
            await focus_schedules.advance(SUBSCRIPTION, now=start + timedelta(minutes=3 * tick))
        assert submissions == ["2030-03-01T00:00:00Z"] * 6 + ["2030-04-01T00:00:00Z", "2030-05-01T00:00:00Z"]
        stored = focus_schedules.StoredSchedule.model_validate_json(next(iter(store.documents.values())))
        assert stored.cycle.status == "running"
        assert [month.attempts for month in stored.cycle.months[:3]] == [1, 1, 1]
        assert stored.cycle.months[0].throttled_since is None

    asyncio.run(exercise())


def test_a_day_of_throttling_fails_the_refresh_with_a_clear_reason(store, monkeypatch):
    original = focus_schedules._native_request

    async def always_throttled(method, subscription_id, suffix="", body=None, kind="monthly"):
        result = await original(method, subscription_id, suffix, body, kind)
        if suffix == "run":
            raise _throttled({"x-ms-ratelimit-microsoft.consumption-retry-after": "900"})
        return result

    monkeypatch.setattr(focus_schedules, "_native_request", always_throttled)

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        start = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=start)
        await focus_schedules.advance(SUBSCRIPTION, now=start + timedelta(hours=23))
        assert (await focus_schedules.load(SUB))["latestRun"]["status"] == "running"
        await focus_schedules.advance(SUBSCRIPTION, now=start + timedelta(hours=24, minutes=1))
        run = (await focus_schedules.load(SUB))["latestRun"]
        assert run["status"] == "failed" and "throttled" in run["error"]

    asyncio.run(exercise())

@pytest.mark.parametrize("day, expected", [
    ("2030-10-01", [("2030-09-01T00:00:00+00:00", "2030-09-30T23:59:59+00:00")]),
    ("2030-10-03", [("2030-09-01T00:00:00+00:00", "2030-09-30T23:59:59+00:00"), ("2030-10-01T00:00:00+00:00", "2030-10-02T23:59:59+00:00")]),
    ("2030-10-06", [("2030-10-01T00:00:00+00:00", "2030-10-05T23:59:59+00:00")]),
    ("2031-01-02", [("2030-12-01T00:00:00+00:00", "2030-12-31T23:59:59+00:00"), ("2031-01-01T00:00:00+00:00", "2031-01-01T23:59:59+00:00")]),
])
def test_daily_periods_cover_the_month_to_yesterday_and_last_month_early_on(day, expected):
    periods = focus_schedules.daily_periods(datetime.fromisoformat(day).date())
    assert [(period.start.isoformat(), period.end.isoformat()) for period in periods] == expected


def _daily_posts(store):
    return [call for call in store.requests if call[0] == "POST" and call[4] == "daily"]


def _complete(runs, name, period):
    runs.append({"name": name, "properties": {"status": "Completed", "startDate": period["from"], "endDate": period["to"]}})


def test_daily_pull_runs_once_a_day_after_its_utc_time(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_time="06:00")
        day = datetime(2030, 10, 10, tzinfo=timezone.utc)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=day + timedelta(hours=5, minutes=59)))["status"] == "not_due"
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=day + timedelta(hours=6)))["status"] == "queued"
        period = _daily_posts(store)[-1][3]["timePeriod"]
        assert period == {"from": "2030-10-01T00:00:00Z", "to": "2030-10-09T23:59:59Z"}
        _complete(store.daily_runs, "daily-1", period)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=day + timedelta(hours=6, minutes=5)))["status"] == "succeeded"
        daily = (await focus_schedules.load(SUB))["daily"]
        assert daily["status"] == "succeeded" and daily["dataThrough"] == "2030-10-09" and daily["timeUtc"] == "06:00"
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=day + timedelta(hours=23)))["status"] == "not_due"
        await focus_schedules.advance_daily(SUBSCRIPTION, now=day + timedelta(days=1, hours=6))
        assert _daily_posts(store)[-1][3]["timePeriod"] == {"from": "2030-10-01T00:00:00Z", "to": "2030-10-10T23:59:59Z"}
        assert len(_daily_posts(store)) == 2
        assert not [call for call in store.requests if call[0] == "POST" and call[4] != "daily"]

    asyncio.run(exercise())


def test_daily_pull_refreshes_last_month_first_during_the_first_five_days(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 10, 3, 7, tzinfo=timezone.utc)
        await focus_schedules.advance_daily(SUBSCRIPTION, now=now)
        previous = _daily_posts(store)[-1][3]["timePeriod"]
        assert previous == {"from": "2030-09-01T00:00:00Z", "to": "2030-09-30T23:59:59Z"}
        _complete(store.daily_runs, "september", previous)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=5)))["status"] == "queued"
        current = _daily_posts(store)[-1][3]["timePeriod"]
        assert current == {"from": "2030-10-01T00:00:00Z", "to": "2030-10-02T23:59:59Z"}
        _complete(store.daily_runs, "october", current)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=10)))["status"] == "succeeded"
        assert (await focus_schedules.load(SUB))["daily"]["dataThrough"] == "2030-10-02"

    asyncio.run(exercise())


def test_daily_pull_waits_an_hour_when_the_daily_export_is_missing(store, monkeypatch):
    original = focus_schedules._native_request

    async def missing_daily(method, subscription_id, suffix="", body=None, kind="monthly"):
        if kind == "daily" and method == "GET" and not suffix:
            raise httpx.HTTPStatusError("missing", request=httpx.Request("GET", "https://management.azure.com"), response=httpx.Response(404))
        return await original(method, subscription_id, suffix, body, kind)

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        monkeypatch.setattr(focus_schedules, "_native_request", missing_daily)
        now = datetime(2030, 10, 10, 6, tzinfo=timezone.utc)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now))["status"] == "unavailable"
        daily = (await focus_schedules.load(SUB))["daily"]
        assert daily["status"] == "unavailable" and "Configure export" in daily["error"]
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=30)))["status"] == "not_due"
        monkeypatch.setattr(focus_schedules, "_native_request", original)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=61)))["status"] == "queued"
        assert len(_daily_posts(store)) == 1

    asyncio.run(exercise())


def test_daily_pull_is_skipped_when_disabled_or_the_schedule_is_paused(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_enabled=False)
        now = datetime(2030, 10, 10, 7, tzinfo=timezone.utc)
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now))["status"] == "not_due"
        await focus_schedules.update(SUB, ACTOR, "paused", daily_enabled=True, daily_time="05:30")
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now))["status"] == "not_due"
        daily = (await focus_schedules.load(SUB))["daily"]
        assert daily["enabled"] is True and daily["timeUtc"] == "05:30" and daily["status"] == "idle"
        assert not [call for call in store.requests if call[4] == "daily"]

    asyncio.run(exercise())


def test_long_run_history_is_reconciled_by_period(store):
    for index in range(150):
        day = date(2030, 1, 1) + timedelta(days=index)
        store.daily_runs.append({"name": f"old-{index}", "properties": {
            "status": "Completed", "startDate": f"{day:%Y-%m}-01T00:00:00Z", "endDate": f"{day}T23:59:59Z"}})

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z")
        now = datetime(2030, 10, 10, 6, tzinfo=timezone.utc)
        await focus_schedules.advance_daily(SUBSCRIPTION, now=now)
        stored = focus_schedules.StoredSchedule.model_validate_json(next(iter(store.documents.values())))
        assert stored.daily.runs[0].baseline_ids == []
        _complete(store.daily_runs, "today", _daily_posts(store)[-1][3]["timePeriod"])
        assert (await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=5)))["status"] == "succeeded"

    asyncio.run(exercise())


def _finish_cycle(store, now):
    for call in [call for call in store.requests if call[0] == "POST" and call[4] == "monthly"]:
        period = call[3]["timePeriod"]
        if not any(run["properties"].get("startDate") == period["from"] for run in store.native_runs):
            _complete(store.native_runs, f"run-{period['from']}-{now:%Y%m%d%H%M}", period)


def test_scheduled_close_repulls_only_months_that_were_not_final(store, monkeypatch):
    monkeypatch.setenv("FOCUS_EXPORT_PARALLEL_MONTHS", "3")

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", 3, daily_enabled=False)
        early = datetime(2030, 9, 2, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=early, force=True)
        _finish_cycle(store, early)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=early + timedelta(minutes=5)))["status"] == "succeeded"
        # August was pulled before Azure finalized it, so the due close pulls only August.
        due = datetime(2030, 9, 5, 4, tzinfo=timezone.utc)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=due))["status"] == "queued"
        posts = [call[3]["timePeriod"]["from"] for call in store.requests if call[0] == "POST"]
        assert posts[3:] == ["2030-08-01T00:00:00Z"]
        latest = (await focus_schedules.load(SUB))["latestRun"]
        assert latest["kind"] == "close" and latest["months"] == [{"period": "2030-08", "status": "queued"}]
        store.native_runs.clear()
        _finish_cycle(store, due)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=due + timedelta(minutes=5)))["status"] == "succeeded"
        assert (await focus_schedules.load(SUB))["nextRunAt"] == "2030-10-05T03:00:00Z"
        await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 10, 5, 4, tzinfo=timezone.utc))
        posts = [call[3]["timePeriod"]["from"] for call in store.requests if call[0] == "POST"]
        assert posts[4:] == ["2030-09-01T00:00:00Z"]

    asyncio.run(exercise())


def test_scheduled_close_with_nothing_to_pull_only_moves_the_schedule(store, monkeypatch):
    monkeypatch.setenv("FOCUS_EXPORT_PARALLEL_MONTHS", "6")

    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_enabled=False)
        refresh = datetime(2030, 10, 4, 4, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=refresh, force=True)
        _finish_cycle(store, refresh)
        assert (await focus_schedules.advance(SUBSCRIPTION, now=refresh + timedelta(minutes=5)))["status"] == "succeeded"
        posts = len([call for call in store.requests if call[0] == "POST"])
        assert (await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 10, 5, 4, tzinfo=timezone.utc)))["status"] == "not_due"
        view = await focus_schedules.load(SUB)
        assert view["nextRunAt"] == "2030-11-05T03:00:00Z" and view["latestRun"]["kind"] == "full"
        assert len([call for call in store.requests if call[0] == "POST"]) == posts

    asyncio.run(exercise())


def _stored(store):
    return focus_schedules.StoredSchedule.model_validate_json(next(iter(store.documents.values())))


def test_daily_view_lists_each_period_and_the_next_pull(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_time="06:00")
        before = datetime(2030, 10, 5, 5, tzinfo=timezone.utc)
        idle = focus_schedules.schedule_view(SUB, _stored(store), now=before)["daily"]
        assert idle["runs"] == [] and idle["day"] is None and idle["nextRunAt"] == "2030-10-05T06:00:00Z"
        now = datetime(2030, 10, 5, 6, 30, tzinfo=timezone.utc)
        await focus_schedules.advance_daily(SUBSCRIPTION, now=now)
        running = focus_schedules.schedule_view(SUB, _stored(store), now=now)["daily"]
        # Day 5 still re-pulls last month for late charges, one period at a time by default.
        assert running["day"] == "2030-10-05" and running["nextRunAt"] is None
        assert running["runs"] == [{"start": "2030-09-01", "end": "2030-09-30", "status": "queued"},
                                   {"start": "2030-10-01", "end": "2030-10-04", "status": "pending"}]
        for minutes in (5, 10):
            _complete(store.daily_runs, f"daily-{minutes}", _daily_posts(store)[-1][3]["timePeriod"])
            await focus_schedules.advance_daily(SUBSCRIPTION, now=now + timedelta(minutes=minutes))
        done = focus_schedules.schedule_view(SUB, _stored(store), now=now + timedelta(minutes=15))["daily"]
        assert done["status"] == "succeeded" and done["dataThrough"] == "2030-10-04"
        assert done["completedAt"] == "2030-10-05T06:40:00Z" and done["nextRunAt"] == "2030-10-06T06:00:00Z"
        assert [run["status"] for run in done["runs"]] == ["succeeded", "succeeded"]

    asyncio.run(exercise())


def test_next_daily_pull_follows_retries_and_is_hidden_when_off_or_paused(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_time="06:00")
        now = datetime(2030, 10, 10, 7, tzinfo=timezone.utc)
        record = _stored(store)
        record.daily.status, record.daily.retry_after = "unavailable", now + timedelta(hours=1)
        assert focus_schedules.schedule_view(SUB, record, now=now)["daily"]["nextRunAt"] == "2030-10-10T08:00:00Z"
        record.daily.retry_after = None
        # Past today's time without a pull today: the next worker run starts it.
        assert focus_schedules.schedule_view(SUB, record, now=now)["daily"]["nextRunAt"] == "2030-10-10T06:00:00Z"
        record.daily.enabled = False
        assert focus_schedules.schedule_view(SUB, record, now=now)["daily"]["nextRunAt"] is None
        record.daily.enabled, record.state = True, "paused"
        assert focus_schedules.schedule_view(SUB, record, now=now)["daily"]["nextRunAt"] is None

    asyncio.run(exercise())


def test_next_run_shows_the_following_close_while_a_refresh_runs(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-10-05T03:00:00Z", 3, daily_enabled=False)
        early = datetime(2030, 10, 4, 3, tzinfo=timezone.utc)
        await focus_schedules.advance(SUBSCRIPTION, now=early, force=True)
        # An Export before the run time leaves the scheduled close where it was.
        assert focus_schedules.schedule_view(SUB, _stored(store), now=early)["nextRunAt"] == "2030-10-05T03:00:00Z"
        later = datetime(2030, 10, 5, 3, 10, tzinfo=timezone.utc)
        assert focus_schedules.schedule_view(SUB, _stored(store), now=later)["nextRunAt"] == "2030-11-05T03:00:00Z"

    asyncio.run(exercise())


def test_stored_cycles_with_months_outside_their_window_are_rejected(store):
    async def exercise():
        await focus_schedules.create(SUB, ACTOR, "2030-09-05T03:00:00Z", daily_enabled=False)
        await focus_schedules.advance(SUBSCRIPTION, now=datetime(2030, 9, 5, 4, tzinfo=timezone.utc))
        name, raw = next(iter(store.documents.items()))
        store.documents[name] = raw.replace(b'"start":"2030-03-01T00:00:00Z"', b'"start":"2029-03-01T00:00:00Z"', 1)
        view = await focus_schedules.load(SUB)
        assert view["availability"] == "export_unavailable" and "safely read" in view["statusMessage"]

    asyncio.run(exercise())


def test_worker_refreshes_the_report_after_a_daily_pull(store, monkeypatch):
    from jobs import scheduler
    import main

    calls = []

    async def daily_done(subscription_id, **kwargs):
        return {"subscriptionId": subscription_id, "status": "succeeded"}

    async def fake_active_ids():
        return [SUBSCRIPTION]

    async def fake_build_and_publish(subscription_ids, stale_days):
        calls.append(subscription_ids)

    monkeypatch.setattr(focus_schedules, "scheduled_subscription_ids", lambda: [SUBSCRIPTION])
    monkeypatch.setattr(focus_schedules, "advance", not_due)
    monkeypatch.setattr(focus_schedules, "advance_daily", daily_done)
    monkeypatch.setattr(focus_schedules, "active_scheduled_subscription_ids", fake_active_ids)
    monkeypatch.setattr(main, "_build_and_publish_report", fake_build_and_publish)
    assert asyncio.run(scheduler.run_once()) == 0
    assert calls == [[SUBSCRIPTION]]
