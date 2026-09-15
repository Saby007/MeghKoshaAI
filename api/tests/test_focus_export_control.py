import asyncio
import pytest

from services import focus_export_control

SUBSCRIPTION_ID = "616dc9b8-b4aa-415f-8dcb-71bc462916c5"


def test_export_path_uses_environment_export_name(monkeypatch):
    monkeypatch.setenv("COST_EXPORT_NAME", "focus-closed-month-meghkoshaai-dev")

    assert focus_export_control._export_path(SUBSCRIPTION_ID) == (
        f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.CostManagement/"
        "exports/focus-closed-month-meghkoshaai-dev?api-version=2025-03-01"
    )


def _export(status="Active"):
    return {
        "eTag": '"etag-1"',
        "location": "eastus2",
        "identity": {"type": "SystemAssigned", "principalId": "principal-1"},
        "properties": {
            "compressionMode": "gzip",
            "definition": {
                "type": "FocusCost",
                "timeframe": "TheLastMonth",
                "dataSet": {"granularity": "Daily", "configuration": {"dataVersion": "1.2-preview"}},
            },
            "deliveryInfo": {
                "destination": {
                    "type": "AzureBlob",
                    "resourceId": "/subscriptions/host/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/store",
                    "container": "cost-exports",
                    "rootFolderPath": f"focus/{SUBSCRIPTION_ID}",
                }
            },
            "schedule": {
                "status": status,
                "recurrence": "Monthly",
                "recurrencePeriod": {"from": "2030-09-05T03:00:00Z", "to": "2040-09-05T03:00:00Z"},
            },
        },
    }


def test_update_payload_preserves_identity_destination_and_etag():
    body = focus_export_control._update_body(
        _export(),
        status="Inactive",
        schedule_start="2031-01-05T04:30:00Z",
    )

    assert body["eTag"] == '"etag-1"'
    assert body["identity"] == {"type": "SystemAssigned"}
    assert body["location"] == "eastus2"
    assert body["properties"]["definition"]["type"] == "FocusCost"
    assert body["properties"]["deliveryInfo"]["destination"]["rootFolderPath"] == f"focus/{SUBSCRIPTION_ID}"
    assert body["properties"]["schedule"]["status"] == "Inactive"
    assert body["properties"]["schedule"]["recurrencePeriod"]["from"] == "2031-01-05T04:30:00Z"


def test_create_starts_inactive_then_activates(monkeypatch):
    calls = []

    async def missing(_subscription_id):
        return None

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return {}

    async def activate(subscription_id, **kwargs):
        calls.append(("ACTIVATE", subscription_id, kwargs))
        return _export()

    monkeypatch.setenv(
        "COST_EXPORT_STORAGE_RESOURCE_ID",
        "/subscriptions/host/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/store",
    )
    monkeypatch.setattr(focus_export_control, "get_export", missing)
    monkeypatch.setattr(focus_export_control.arm_client, "_arm_request", request)
    monkeypatch.setattr(focus_export_control, "update_export", activate)

    result = asyncio.run(focus_export_control.create_export(SUBSCRIPTION_ID, "2030-09-05T03:00:00Z"))

    assert result["properties"]["schedule"]["status"] == "Active"
    assert calls[0][0] == "PUT"
    assert calls[0][2]["properties"]["schedule"]["status"] == "Inactive"
    assert calls[1] == ("ACTIVATE", SUBSCRIPTION_ID, {"status": "Active"})


def test_run_uses_optional_empty_body(monkeypatch):
    calls = []

    async def get(_subscription_id):
        return _export()

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    monkeypatch.setattr(focus_export_control, "get_export", get)
    monkeypatch.setattr(focus_export_control.arm_client, "_arm_request", request)

    result = asyncio.run(focus_export_control.run_export(SUBSCRIPTION_ID))

    assert result == {"subscriptionId": SUBSCRIPTION_ID, "status": "queued"}
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/run?api-version=2025-03-01")
    assert "json" not in calls[0][2]


def test_export_view_uses_native_schedule_and_latest_run():
    view = focus_export_control.export_view(
        SUBSCRIPTION_ID,
        "Subscription One",
        _export("Active"),
        {"runId": "run-1", "status": "Completed"},
    )

    assert view["state"] == "active"
    assert view["scheduleStartAt"] == "2030-09-05T03:00:00Z"
    assert view["nextRunAt"] == "2030-09-05T03:00:00Z"
    assert view["latestRun"]["runId"] == "run-1"


def test_run_view_reports_native_duration():
    view = focus_export_control.run_view(
        {
            "name": "run-1",
            "properties": {
                "status": "Completed",
                "startDate": "2026-07-01T00:00:00Z",
                "processingStartTime": "2026-08-16T08:18:47Z",
                "processingEndTime": "2026-08-16T08:20:37Z",
            },
        }
    )

    assert view["status"] == "succeeded"
    assert view["durationSeconds"] == 110


@pytest.mark.parametrize("status", [None, "UnknownNativeState", "Cancelled"])
def test_unknown_run_states_are_not_success_or_queued(status):
    assert focus_export_control.run_view({"properties": {"status": status}})["status"] == "unknown"


@pytest.mark.parametrize("status", [None, "UnknownNativeState"])
def test_missing_export_status_is_not_reported_as_paused(status):
    assert focus_export_control.export_view(SUBSCRIPTION_ID, "Sub", _export(status), None)["state"] == "unknown"


def test_malformed_active_schedule_has_no_invented_next_run():
    export = _export()
    export["properties"]["schedule"]["recurrencePeriod"]["from"] = "invalid-date"
    view = focus_export_control.export_view(SUBSCRIPTION_ID, "Sub", export, None)
    assert view["state"] == "unknown"
    assert view["nextRunAt"] is None
