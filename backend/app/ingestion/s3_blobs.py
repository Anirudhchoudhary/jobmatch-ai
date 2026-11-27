"""S3-backed blob store with SigV4 presigned PUT uploads."""

import asyncio
from typing import Any

from botocore.exceptions import ClientError

from app.ingestion.blobs import _not_found

_CONTENT_TYPE = "application/pdf"
_SSE = "AES256"


class S3BlobStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=_CONTENT_TYPE,
            ServerSideEncryption=_SSE,
        )

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)

    def _get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = resp["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise _not_found() from None
            raise
        return body

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    def presign_put(self, key: str, size_bytes: int, expires_s: int = 300) -> str:
        url: str = self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ContentType": _CONTENT_TYPE,
                "ContentLength": size_bytes,
                "ServerSideEncryption": _SSE,
            },
            ExpiresIn=expires_s,
            HttpMethod="PUT",
        )
        return url
