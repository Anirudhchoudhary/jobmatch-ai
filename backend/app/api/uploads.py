"""Upload tickets and the local-only PUT target (S3 presigned URL in Phase 7)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_current_user_id
from app.core.errors import AppError
from app.core.settings import get_settings
from app.ingestion.blobs import BlobStore, assert_owned_key, get_blob_store, new_upload_key
from app.schemas.documents import UploadRequest, UploadTicket

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
local_put_router = APIRouter(prefix="/api/uploads", tags=["uploads"])

UserId = Annotated[UUID, Depends(get_current_user_id)]


def _too_large() -> AppError:
    return AppError("too_large", "That file is too large (5 MB maximum).", 413)


@router.post("")
async def create_upload(body: UploadRequest, user_id: UserId) -> UploadTicket:
    max_bytes = get_settings().max_upload_bytes
    if body.size_bytes > max_bytes:
        raise _too_large()
    key = new_upload_key(user_id)
    return UploadTicket(
        key=key,
        url=f"/api/uploads/{key}",
        method="PUT",
        headers={"Content-Type": "application/pdf"},
        max_bytes=max_bytes,
    )


@local_put_router.put("/{key:path}", status_code=204)
async def put_upload(
    key: str,
    request: Request,
    user_id: UserId,
    store: Annotated[BlobStore, Depends(get_blob_store)],
) -> Response:
    assert_owned_key(key, user_id)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/pdf":
        raise AppError("unsupported_media_type", "Upload a PDF file.", 415)
    max_bytes = get_settings().max_upload_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > max_bytes:
        raise _too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _too_large()
        chunks.append(chunk)
    await store.put(key, b"".join(chunks))
    return Response(status_code=204)
