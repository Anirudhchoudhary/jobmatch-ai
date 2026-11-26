import io
import json
import math
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber
from structlog.testing import capture_logs

from app.core.errors import ProviderError
from app.llm.embeddings import BedrockTitanEmbedder, HashEmbedder

MODEL = "amazon.titan-embed-text-v2:0"


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_embedder_deterministic_normalized() -> None:
    e = HashEmbedder()
    a = await e.embed_query("Python, Kafka and AWS")
    b = await e.embed_query("Python, Kafka and AWS")
    assert a == b
    assert len(a) == 1024
    assert abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-6


async def test_hash_embedder_empty_text_is_unit_vector() -> None:
    v = await HashEmbedder().embed_query("")
    assert abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-6


async def test_hash_embedder_similarity() -> None:
    e = HashEmbedder()
    q = await e.embed_query("python kafka aws")
    near = await e.embed_query("aws kafka")
    far = await e.embed_query("pastry baking")
    assert _cos(q, near) > _cos(q, far)
    assert _cos(q, near) > 0


def _client() -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name="us-west-2",
        aws_access_key_id="x",
        aws_secret_access_key="x",
        config=Config(retries={"max_attempts": 1}),
    )


def _body(vec: list[float]) -> StreamingBody:
    raw = json.dumps({"embedding": vec}).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


async def test_titan_request_and_order() -> None:
    client = _client()
    texts = ["a", "b", "c"]
    with Stubber(client) as stub:
        for i, t in enumerate(texts):
            stub.add_response(
                "invoke_model",
                {"body": _body([float(i), 0.0]), "contentType": "application/json"},
                {
                    "modelId": MODEL,
                    "body": json.dumps({"inputText": t, "dimensions": 2, "normalize": True}),
                    "contentType": "application/json",
                    "accept": "application/json",
                },
            )
        emb = BedrockTitanEmbedder(client, MODEL, 2)
        out = await emb.embed_documents(texts)
    assert out == [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]


async def test_titan_error_becomes_provider_error() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("invoke_model", "ThrottlingException", "slow down")
        emb = BedrockTitanEmbedder(client, MODEL, 2)
        with pytest.raises(ProviderError) as exc:
            await emb.embed_query("x")
    assert "Throttling" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


async def test_titan_malformed_response_becomes_provider_error() -> None:
    client = _client()
    raw = b'{"nope": 1}'
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(raw), len(raw)), "contentType": "application/json"},
        )
        with pytest.raises(ProviderError):
            await BedrockTitanEmbedder(client, MODEL, 2).embed_query("x")
    bad = b"not json"
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(bad), len(bad)), "contentType": "application/json"},
        )
        with pytest.raises(ProviderError):
            await BedrockTitanEmbedder(client, MODEL, 2).embed_query("x")


def test_embed_model_identifiers() -> None:
    assert HashEmbedder().embed_model == "hash:v1:1024"
    assert BedrockTitanEmbedder(_client(), MODEL, 1024).embed_model == f"bedrock:{MODEL}:1024"


async def test_titan_wrong_vector_length_becomes_provider_error() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model", {"body": _body([1.0, 0.0, 0.0]), "contentType": "application/json"}
        )
        with pytest.raises(ProviderError):
            await BedrockTitanEmbedder(client, MODEL, 2).embed_query("x")


async def test_titan_error_logs_code_without_message() -> None:
    client = _client()
    with Stubber(client) as stub, capture_logs() as logs:
        stub.add_client_error("invoke_model", "ThrottlingException", "ZQXSLOWDOWN")
        with pytest.raises(ProviderError):
            await BedrockTitanEmbedder(client, MODEL, 2).embed_query("ZQXINPUT")
    (entry,) = [e for e in logs if e["event"] == "provider_call_failed"]
    assert entry["log_level"] == "warning"
    assert entry["provider"] == "bedrock"
    assert entry["error_type"] == "ThrottlingException"  # botocore's modeled ClientError subclass
    assert entry["error_code"] == "ThrottlingException"
    assert "ZQXSLOWDOWN" not in str(logs) and "ZQXINPUT" not in str(logs)
