import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException

os.environ.setdefault("AI_PROJECT_ENDPOINT", "https://example.test/api/projects/test")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")

import main
from services import focus_export_download
from tests.test_control_api import FakeRequest


SUBSCRIPTION_ID = "616dc9b8-b4aa-415f-8dcb-71bc462916c5"
PREFIX = f"focus/{SUBSCRIPTION_ID}/"


class FakeDownloader:
    def __init__(self, chunks):
        self._chunks = chunks

    def chunks(self):
        return iter(self._chunks)


class FakeBlobClient:
    def __init__(self, name, content):
        self.name = name
        self.content = content

    def get_blob_properties(self):
        return SimpleNamespace(
            size=len(self.content),
            last_modified=datetime(2026, 8, 16, tzinfo=timezone.utc),
            content_settings=SimpleNamespace(content_type="application/gzip"),
        )


class FakeContainer:
    def __init__(self):
        self.content = b"focus-data"
        self.items = [
            SimpleNamespace(
                name=f"{PREFIX}20260701-20260731/part-00000.csv.gz",
                size=len(self.content),
                last_modified=datetime(2026, 8, 16, tzinfo=timezone.utc),
                content_settings=SimpleNamespace(content_type="application/gzip"),
            ),
            SimpleNamespace(
                name=f"{PREFIX}20260701-20260731/manifest.json",
                size=2,
                last_modified=datetime(2026, 8, 16, tzinfo=timezone.utc),
                content_settings=SimpleNamespace(content_type="application/json"),
            ),
        ]

    def list_blobs(self, name_starts_with):
        return [item for item in self.items if item.name.startswith(name_starts_with)]

    def get_blob_client(self, name):
        return FakeBlobClient(name, self.content)

    def download_blob(self, name):
        return FakeDownloader([self.content[:5], self.content[5:]])


def test_lists_only_downloadable_focus_files(monkeypatch):
    monkeypatch.setattr(focus_export_download, "get_container_client", lambda: FakeContainer())

    files = asyncio.run(focus_export_download.list_focus_files(SUBSCRIPTION_ID))

    assert [item.file_name for item in files] == ["part-00000.csv.gz"]
    assert files[0].size == len(b"focus-data")


def test_rejects_cross_subscription_and_non_csv_paths():
    with pytest.raises(focus_export_download.FocusExportPathError):
        focus_export_download._validate_blob_name(
            SUBSCRIPTION_ID,
            "focus/6efdb685-f5ce-4be4-9841-83d09c4ac05a/part.csv.gz",
        )
    with pytest.raises(focus_export_download.FocusExportPathError):
        focus_export_download._validate_blob_name(SUBSCRIPTION_ID, f"{PREFIX}manifest.json")


def test_streams_focus_file_without_buffering_the_whole_blob(monkeypatch):
    monkeypatch.setattr(focus_export_download, "get_container_client", lambda: FakeContainer())

    async def collect():
        return [chunk async for chunk in focus_export_download.stream_focus_file(
            SUBSCRIPTION_ID,
            f"{PREFIX}20260701-20260731/part-00000.csv.gz",
        )]

    assert asyncio.run(collect()) == [b"focus", b"-data"]


async def _visible_subscription(subscription_id):
    assert subscription_id == SUBSCRIPTION_ID
    return {"subscriptionId": subscription_id, "displayName": "Subscription One"}


def test_list_endpoint_requires_onboarded_subscription_and_serializes_files(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(main.arm_client, "get_subscription", _visible_subscription)

    async def list_files(subscription_id):
        assert subscription_id == SUBSCRIPTION_ID
        return [
            focus_export_download.FocusExportFile(
                blob_name=f"{PREFIX}20260701-20260731/part-00000.csv.gz",
                file_name="part-00000.csv.gz",
                size=10,
                last_modified="2026-08-16T08:00:00+00:00",
                content_type="application/gzip",
            )
        ]

    monkeypatch.setattr(main.focus_export_download, "list_focus_files", list_files)

    result = asyncio.run(main.list_focus_exports(FakeRequest(), SUBSCRIPTION_ID))

    assert result["subscriptionId"] == SUBSCRIPTION_ID
    assert result["files"][0]["fileName"] == "part-00000.csv.gz"


def test_list_endpoint_rejects_unknown_subscription(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")

    async def missing(subscription_id):
        request = httpx.Request("GET", f"https://management.azure.com/subscriptions/{subscription_id}")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("missing", request=request, response=response)

    monkeypatch.setattr(main.arm_client, "get_subscription", missing)

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.list_focus_exports(FakeRequest(), SUBSCRIPTION_ID))

    assert error.value.status_code == 404


def test_list_endpoint_keeps_history_available_after_export_deletion(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(main.arm_client, "get_subscription", _visible_subscription)
    monkeypatch.setattr(main.focus_export_download, "list_focus_files", lambda subscription_id: None)

    async def no_files(subscription_id):
        return []

    monkeypatch.setattr(main.focus_export_download, "list_focus_files", no_files)

    result = asyncio.run(main.list_focus_exports(FakeRequest(), SUBSCRIPTION_ID))

    assert result == {"subscriptionId": SUBSCRIPTION_ID, "files": []}


def test_download_endpoint_streams_validated_file(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(main.arm_client, "get_subscription", _visible_subscription)
    blob_name = f"{PREFIX}20260701-20260731/part-00000.csv.gz"
    item = focus_export_download.FocusExportFile(
        blob_name=blob_name,
        file_name="part-00000.csv.gz",
        size=10,
        last_modified="2026-08-16T08:00:00+00:00",
        content_type="application/gzip",
    )

    async def get_file(subscription_id, requested_blob_name):
        assert subscription_id == SUBSCRIPTION_ID
        assert requested_blob_name == blob_name
        return item

    async def stream_file(subscription_id, requested_blob_name):
        yield b"focus-data"

    monkeypatch.setattr(main.focus_export_download, "get_focus_file", get_file)
    monkeypatch.setattr(main.focus_export_download, "stream_focus_file", stream_file)

    response = asyncio.run(main.download_focus_export(FakeRequest(), SUBSCRIPTION_ID, blob_name))

    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(collect()) == b"focus-data"
    assert response.headers["content-disposition"] == 'attachment; filename="part-00000.csv.gz"'
    assert response.headers["cache-control"] == "private, no-store"