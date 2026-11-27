from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.core.errors import AppError
from app.ingestion.blobs import assert_owned_key, new_upload_key
from app.ingestion.s3_blobs import S3BlobStore

BUCKET = "test-uploads"
ME = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


def _client() -> Any:
    return boto3.client(
        "s3",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(signature_version="s3v4"),
    )


async def test_put_get_delete_roundtrip_calls() -> None:
    client = _client()
    key = new_upload_key(ME)
    body = b"%PDF-1.7 hi"
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": key,
                "Body": body,
                "ContentType": "application/pdf",
                "ServerSideEncryption": "AES256",
            },
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(body), len(body))},
            {"Bucket": BUCKET, "Key": key},
        )
        stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": key})
        store = S3BlobStore(client, BUCKET)
        await store.put(key, body)
        assert await store.get(key) == body
        await store.delete(key)
        stub.assert_no_pending_responses()


async def test_get_missing_maps_to_not_found() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        with pytest.raises(AppError) as exc:
            await S3BlobStore(client, BUCKET).get(new_upload_key(ME))
    assert exc.value.code == "upload_not_found"
    assert exc.value.status == 404


async def test_get_other_client_error_propagates() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            await S3BlobStore(client, BUCKET).get(new_upload_key(ME))


def test_presigned_url_signs_content_length_and_type() -> None:
    key = new_upload_key(ME)
    url = S3BlobStore(_client(), BUCKET).presign_put(key, 1234)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path.endswith(key)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    signed = query["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signed
    assert "content-type" in signed
    assert "x-amz-server-side-encryption" in signed
    assert query["X-Amz-Expires"] == ["300"]


def test_assert_owned_key_rejects_foreign_and_outside_uploads() -> None:
    key = new_upload_key(ME)
    assert_owned_key(key, ME)
    for bad in (key, key.replace("uploads/", "other/", 1), f"uploads/{ME}/../x.pdf"):
        with pytest.raises(AppError):
            assert_owned_key(bad, OTHER)
    with pytest.raises(AppError):
        assert_owned_key(key.replace("uploads/", "other/", 1), ME)


def test_deployed_store_presigns_against_the_regional_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A new bucket answers the global name with a 307 for up to a day, and the signature
    # covers the host, so the URL must name the regional endpoint.
    from app.ingestion.blobs import _s3_store

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    _s3_store.cache_clear()
    try:
        store = _s3_store(BUCKET, "us-west-2")
        assert isinstance(store, S3BlobStore)
        url = urlparse(store.presign_put(new_upload_key(ME), 10))
    finally:
        _s3_store.cache_clear()
    assert url.scheme == "https"
    assert url.hostname == f"{BUCKET}.s3.us-west-2.amazonaws.com"
    assert parse_qs(url.query)["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
