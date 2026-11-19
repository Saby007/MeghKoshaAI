"""The no-login Admin workspace: one target, optional credentials, replacement reports."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from services import focus_uploads, target_connection

router = APIRouter(prefix="/api/admin")


@router.get("/configuration")
async def get_configuration():
    configuration = await target_connection.load(required=False)
    return {"connection": configuration.public() if configuration else None}


@router.put("/configuration")
async def save_configuration(body: target_connection.ConnectionInput):
    return {"connection": (await target_connection.save(body)).public()}


@router.get("/uploads")
async def list_uploads():
    return await focus_uploads.catalog()


@router.post("/uploads", status_code=201)
async def create_upload(body: focus_uploads.UploadInput):
    return await focus_uploads.create(body)


@router.get("/uploads/{dataset_id}")
async def get_upload(dataset_id: UUID):
    return await focus_uploads.status(dataset_id)


@router.put("/uploads/{dataset_id}/files/{file_id}")
async def upload_chunk(request: Request, dataset_id: UUID, file_id: UUID, offset: int = Query(ge=0)):
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > focus_uploads.CHUNK_BYTES:
            raise HTTPException(413, "A file chunk exceeds 8 MiB.")
        content.extend(part)
    return await focus_uploads.write_chunk(dataset_id, file_id, offset, bytes(content))


@router.get("/uploads/{dataset_id}/files/{file_id}/chunks")
async def upload_checkpoints(dataset_id: UUID, file_id: UUID, after: int = Query(default=-1, ge=-1)):
    return await focus_uploads.checkpoints(dataset_id, file_id, after)


@router.post("/uploads/{dataset_id}/discard", status_code=204)
async def discard_upload(dataset_id: UUID):
    await focus_uploads.discard(dataset_id)


@router.post("/uploads/{dataset_id}/complete", status_code=202)
async def complete_upload(dataset_id: UUID):
    return await focus_uploads.finalize(dataset_id)


@router.post("/uploads/{dataset_id}/retry", status_code=202)
async def retry_upload(dataset_id: UUID):
    return await focus_uploads.queue(dataset_id, replace=True)
