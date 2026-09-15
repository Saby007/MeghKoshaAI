"""List and stream private FocusCost export files through the authenticated API."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from services import runtime_identity
from services.focus_export_control import normalize_subscription_id

_DOWNLOAD_SUFFIXES = (".csv", ".csv.gz")


class FocusExportFileNotFoundError(LookupError):
    pass


class FocusExportPathError(ValueError):
    pass


@dataclass(frozen=True)
class FocusExportFile:
    blob_name: str
    file_name: str
    size: int
    last_modified: str
    content_type: str


@lru_cache(maxsize=1)
def get_container_client() -> ContainerClient:
    credential = runtime_identity.credential()
    service = BlobServiceClient(os.environ["COST_EXPORT_STORAGE_URL"], credential=credential)
    return service.get_container_client(os.environ.get("COST_EXPORT_CONTAINER", "cost-exports"))


def _prefix(subscription_id: str) -> str:
    return f"focus/{normalize_subscription_id(subscription_id)}/"


def _validate_blob_name(subscription_id: str, blob_name: str) -> str:
    prefix = _prefix(subscription_id)
    normalized = str(PurePosixPath(blob_name))
    if normalized != blob_name or not blob_name.startswith(prefix) or not blob_name.lower().endswith(_DOWNLOAD_SUFFIXES):
        raise FocusExportPathError("FocusCost file path is invalid")
    return blob_name


def _list_files(subscription_id: str) -> list[FocusExportFile]:
    files: list[FocusExportFile] = []
    for blob in get_container_client().list_blobs(name_starts_with=_prefix(subscription_id)):
        if not blob.name.lower().endswith(_DOWNLOAD_SUFFIXES):
            continue
        content_settings = getattr(blob, "content_settings", None)
        files.append(
            FocusExportFile(
                blob_name=blob.name,
                file_name=PurePosixPath(blob.name).name,
                size=int(blob.size or 0),
                last_modified=blob.last_modified.isoformat() if blob.last_modified else "",
                content_type=(getattr(content_settings, "content_type", None) or "application/gzip"),
            )
        )
    return sorted(files, key=lambda item: (item.last_modified, item.blob_name), reverse=True)


async def list_focus_files(subscription_id: str) -> list[FocusExportFile]:
    return await asyncio.to_thread(_list_files, subscription_id)


def _get_file(subscription_id: str, blob_name: str) -> FocusExportFile:
    blob_name = _validate_blob_name(subscription_id, blob_name)
    blob = get_container_client().get_blob_client(blob_name)
    try:
        properties = blob.get_blob_properties()
    except ResourceNotFoundError as error:
        raise FocusExportFileNotFoundError(blob_name) from error
    return FocusExportFile(
        blob_name=blob_name,
        file_name=PurePosixPath(blob_name).name,
        size=int(properties.size or 0),
        last_modified=properties.last_modified.isoformat() if properties.last_modified else "",
        content_type=properties.content_settings.content_type or "application/gzip",
    )


async def get_focus_file(subscription_id: str, blob_name: str) -> FocusExportFile:
    return await asyncio.to_thread(_get_file, subscription_id, blob_name)


async def stream_focus_file(subscription_id: str, blob_name: str):
    blob_name = _validate_blob_name(subscription_id, blob_name)
    downloader = await asyncio.to_thread(get_container_client().download_blob, blob_name)
    chunks = downloader.chunks()
    while True:
        chunk = await asyncio.to_thread(next, chunks, None)
        if chunk is None:
            break
        yield chunk