"""Upload keys and blob storage. Ownership of keys is security-critical."""

import asyncio
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from app.core.errors import AppError
from app.core.settings import get_settings


def _not_found() -> AppError:
    return AppError("upload_not_found", "Upload not found.", 404)


def new_upload_key(user_id: UUID) -> str:
    return f"uploads/{user_id}/{uuid4()}.pdf"


def assert_owned_key(key: str, user_id: UUID) -> None:
    """Same 404 whether the key is foreign or malformed."""
    if not re.fullmatch(rf"uploads/{re.escape(str(user_id))}/[0-9a-f-]{{36}}\.pdf", key):
        raise _not_found()


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes:
        """Raises ``upload_not_found`` when the key does not exist."""
        ...

    async def delete(self, key: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not key or not path.is_relative_to(self._root) or path == self._root:
            raise _not_found()
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        await asyncio.to_thread(self._write_atomic, path, data)

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise _not_found() from None

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)


@lru_cache
def _s3_store(bucket: str, region: str) -> BlobStore:
    import boto3
    from botocore.config import Config

    from app.ingestion.s3_blobs import S3BlobStore

    # The regional endpoint: a new bucket 307-redirects the global name for up to a day, and a
    # presigned URL's signature covers the host, so the browser could not follow the redirect.
    client = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    return S3BlobStore(client, bucket)


def get_blob_store() -> BlobStore:
    settings = get_settings()
    if settings.blob_backend == "s3" and settings.uploads_bucket:  # bucket enforced by settings
        return _s3_store(settings.uploads_bucket, settings.aws_region)
    return LocalBlobStore(settings.upload_dir)
