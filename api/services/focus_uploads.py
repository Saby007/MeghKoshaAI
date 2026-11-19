"""Resumable, workspace-owned FOCUS files staged in private Blob Storage."""

import base64
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from azure.storage.blob.aio import BlobServiceClient
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from services import assessment_store, runtime_identity, target_connection, workspace
from services.assessment_settings import settings

CHUNK_BYTES = 8 * 1024**2


class FileInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=64 * 1024**3)

    @field_validator("name")
    @classmethod
    def safe_csv_name(cls, value: str) -> str:
        if not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]+', value) or not value.lower().endswith((".csv", ".csv.gz", ".gz")):
            raise ValueError("Select FOCUS CSV or gzip CSV files, without directory paths.")
        return value


class UploadInput(BaseModel):
    files: list[FileInput] = Field(min_length=1, max_length=200)


def blob_name(dataset_id: UUID, file_id: UUID) -> str:
    return f"workspaces/{workspace.namespace()}/{dataset_id}/{file_id}"


def block_id(offset: int) -> str:
    return base64.b64encode(f"{offset:020d}".encode("ascii")).decode("ascii")


@asynccontextmanager
async def container():
    url = os.environ.get("COST_EXPORT_STORAGE_URL")
    if not url:
        raise RuntimeError("Private upload storage is not configured.")
    async with runtime_identity.async_credential() as identity:
        async with BlobServiceClient(account_url=url, credential=identity) as service:
            yield service.get_container_client(os.environ.get("FOCUS_UPLOAD_CONTAINER", "focus-uploads"))


async def create(body: UploadInput) -> dict:
    configuration = await target_connection.load()
    if configuration is None:
        raise HTTPException(409, "Set the target subscription first.")
    if sum(item.size for item in body.files) > settings().input_bytes:
        raise HTTPException(413, "This report exceeds the configured upload-byte budget. Contact the deployment operator.")
    dataset_id = uuid4()
    db = await assessment_store.pool(for_read=False)
    async with db.acquire(timeout=10) as connection, connection.transaction():
        await connection.execute(
            "SELECT workspace_id FROM assessments.workspaces WHERE workspace_id=$1 FOR UPDATE",
            UUID(workspace.namespace()),
        )
        count = await connection.fetchval(
            "SELECT count(*) FROM assessments.uploads WHERE workspace_id=$1 AND status='uploading'",
            UUID(workspace.namespace()),
        )
        if count >= 5:
            raise HTTPException(409, "This workspace already has five incomplete uploads. Resume an existing upload.")
        await connection.execute(
            "INSERT INTO assessments.uploads(dataset_id,workspace_id,subscription_id) VALUES($1,$2,$3)",
            dataset_id, UUID(workspace.namespace()), configuration.subscription_id,
        )
        await connection.executemany(
            "INSERT INTO assessments.upload_files(file_id,dataset_id,ordinal,name,total_bytes) VALUES($1,$2,$3,$4,$5)",
            [(uuid4(), dataset_id, ordinal, item.name, item.size) for ordinal, item in enumerate(body.files)],
        )
    return await status(dataset_id)


async def _owned(dataset_id: UUID, *, connection=None, lock: bool = False):
    db = connection if connection is not None else await assessment_store.pool()
    row = await db.fetchrow(
        "SELECT * FROM assessments.uploads WHERE dataset_id=$1 AND workspace_id=$2" + (" FOR UPDATE" if lock else ""),
        dataset_id, UUID(workspace.namespace()),
    )
    if row is None:
        raise HTTPException(404, "Upload not found in this report workspace.")
    return row


async def status(dataset_id: UUID) -> dict:
    row = await _owned(dataset_id)
    files = await (await assessment_store.pool()).fetch(
        "SELECT file_id,name,total_bytes,received_bytes FROM assessments.upload_files "
        "WHERE dataset_id=$1 ORDER BY ordinal", dataset_id,
    )
    job = await assessment_store.get_job(workspace.namespace(), row["job_id"]) if row["job_id"] else None
    return {
        "datasetId": str(dataset_id), "subscriptionId": row["subscription_id"],
        "status": row["status"], "createdAt": row["created_at"].isoformat(),
        "chunkBytes": CHUNK_BYTES, "coverage": json.loads(row["coverage"]) if row["coverage"] else None,
        "error": row["error_message"], "job": job,
        "files": [{"fileId": str(item["file_id"]), "name": item["name"],
                   "size": item["total_bytes"], "receivedBytes": item["received_bytes"]} for item in files],
    }


async def catalog() -> list[dict]:
    rows = await (await assessment_store.pool()).fetch(
        "SELECT dataset_id FROM assessments.uploads WHERE workspace_id=$1 ORDER BY created_at DESC LIMIT 20",
        UUID(workspace.namespace()),
    )
    return [await status(row["dataset_id"]) for row in rows]


async def checkpoints(dataset_id: UUID, file_id: UUID, after: int) -> dict:
    await _owned(dataset_id)
    if not await (await assessment_store.pool()).fetchval(
        "SELECT 1 FROM assessments.upload_files WHERE file_id=$1 AND dataset_id=$2", file_id, dataset_id,
    ):
        raise HTTPException(404, "File not found in this report.")
    rows = await (await assessment_store.pool()).fetch(
        "SELECT byte_offset,byte_length,sha256 FROM assessments.upload_chunks "
        "WHERE file_id=$1 AND byte_offset>$2 ORDER BY byte_offset LIMIT 200", file_id, after,
    )
    return {"items": [{"offset": row["byte_offset"], "length": row["byte_length"], "sha256": row["sha256"]}
                      for row in rows],
            "nextOffset": rows[-1]["byte_offset"] if len(rows) == 200 else None}


async def discard(dataset_id: UUID) -> None:
    await _owned(dataset_id)
    result = await (await assessment_store.pool(for_read=False)).execute(
        "UPDATE assessments.uploads SET status='failed',error_message='Incomplete upload discarded.' "
        "WHERE dataset_id=$1 AND workspace_id=$2 AND status='uploading'",
        dataset_id, UUID(workspace.namespace()),
    )
    if result != "UPDATE 1":
        raise HTTPException(409, "Only an incomplete upload can be discarded.")


async def write_chunk(dataset_id: UUID, file_id: UUID, offset: int, content: bytes) -> dict:
    if not 0 < len(content) <= CHUNK_BYTES:
        raise HTTPException(413, "Upload chunks must contain between one byte and 8 MiB.")
    digest = hashlib.sha256(content).hexdigest()
    db = await assessment_store.pool(for_read=False)
    async with db.acquire(timeout=10) as connection, connection.transaction():
        upload = await _owned(dataset_id, connection=connection, lock=True)
        item = await connection.fetchrow(
            "SELECT * FROM assessments.upload_files WHERE file_id=$1 AND dataset_id=$2 FOR UPDATE",
            file_id, dataset_id,
        )
        if item is None:
            raise HTTPException(404, "File not found in this upload.")
        existing = await connection.fetchrow(
            "SELECT byte_length,sha256 FROM assessments.upload_chunks WHERE file_id=$1 AND byte_offset=$2",
            file_id, offset,
        )
        if existing:
            if existing["byte_length"] != len(content) or existing["sha256"] != digest:
                raise HTTPException(409, "The retried chunk differs from the already uploaded content.")
            return {"receivedBytes": item["received_bytes"]}
        if upload["status"] != "uploading":
            raise HTTPException(409, "This report is finalized and cannot be changed.")
        if offset != item["received_bytes"] or len(content) != min(CHUNK_BYTES, item["total_bytes"] - offset):
            raise HTTPException(409, "The chunk does not match the next expected file offset or length.")
        async with container() as storage:
            await storage.get_blob_client(blob_name(dataset_id, file_id)).stage_block(
                block_id(offset), content, length=len(content), validate_content=True,
            )
        await connection.execute(
            "INSERT INTO assessments.upload_chunks(file_id,byte_offset,byte_length,sha256) VALUES($1,$2,$3,$4)",
            file_id, offset, len(content), digest,
        )
        await connection.execute(
            "UPDATE assessments.upload_files SET received_bytes=received_bytes+$2 WHERE file_id=$1",
            file_id, len(content),
        )
    return {"receivedBytes": offset + len(content)}


async def finalize(dataset_id: UUID) -> dict:
    configuration = await target_connection.load()
    if configuration is None:
        raise HTTPException(409, "Set the target subscription first.")
    db = await assessment_store.pool(for_read=False)
    async with db.acquire(timeout=10) as connection, connection.transaction():
        upload = await _owned(dataset_id, connection=connection, lock=True)
        if upload["subscription_id"] != configuration.subscription_id:
            raise HTTPException(409, "This upload belongs to a different target subscription. Start a new upload.")
        if upload["job_id"] and upload["status"] in {"queued", "processing", "ready"}:
            job_id = upload["job_id"]
        else:
            job_id = None
        if upload["status"] == "uploading":
            files = await connection.fetch(
                "SELECT * FROM assessments.upload_files WHERE dataset_id=$1 ORDER BY ordinal", dataset_id,
            )
            if not files or any(item["received_bytes"] != item["total_bytes"] for item in files):
                raise HTTPException(409, "Upload every file completely before generating the dashboard.")
            async with container() as storage:
                for item in files:
                    chunks = await connection.fetch(
                        "SELECT byte_offset FROM assessments.upload_chunks WHERE file_id=$1 ORDER BY byte_offset",
                        item["file_id"],
                    )
                    await storage.get_blob_client(blob_name(dataset_id, item["file_id"])).commit_block_list(
                        [block_id(chunk["byte_offset"]) for chunk in chunks],
                    )
            await connection.execute(
                "UPDATE assessments.uploads SET status='queued',error_message=NULL WHERE dataset_id=$1", dataset_id,
            )
    if job_id is not None:
        job = await assessment_store.get_job(workspace.namespace(), job_id)
        if job is not None:
            return job
    return await queue(dataset_id, replace=True)


async def queue(dataset_id: UUID, *, replace: bool = False, stale_days: int = 90) -> dict:
    configuration = await target_connection.load()
    upload = await _owned(dataset_id)
    if configuration is None or upload["subscription_id"] != configuration.subscription_id:
        raise HTTPException(409, "The selected report does not match the configured target subscription.")
    if upload["status"] == "uploading":
        raise HTTPException(409, "Complete the upload before processing it.")
    job = await assessment_store.enqueue(
        workspace.namespace(), workspace.namespace(), [configuration.subscription_id], stale_days,
        task_payload={"datasetId": str(dataset_id), "configurationRevision": configuration.revision},
        replace_active=replace,
    )
    return job


async def queue_active(stale_days: int) -> dict:
    configuration = await target_connection.load()
    row = await (await assessment_store.pool()).fetchrow(
        "SELECT u.dataset_id FROM assessments.workspaces w JOIN assessments.uploads u ON u.dataset_id=w.active_dataset "
        "WHERE w.workspace_id=$1 AND u.subscription_id=$2",
        UUID(workspace.namespace()), configuration.subscription_id if configuration else "",
    )
    if row is None:
        raise HTTPException(409, "Upload a FOCUS report in Admin before generating a dashboard.")
    return await queue(row["dataset_id"], stale_days=stale_days)
