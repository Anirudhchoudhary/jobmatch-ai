from typing import Any

import boto3
from botocore.config import Config
from botocore.stub import Stubber

from app.llm.rerank import BedrockReranker, NoopReranker

ARN = "arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0"


def _client() -> Any:
    return boto3.client(
        "bedrock-agent-runtime",
        region_name="us-west-2",
        aws_access_key_id="x",
        aws_secret_access_key="x",
        config=Config(retries={"max_attempts": 1}),
    )


async def test_noop_reranker() -> None:
    hits = await NoopReranker().rerank("q", ["a"] * 5, 3)
    assert [h.index for h in hits] == [0, 1, 2]
    assert all(h.relevance is None for h in hits)


async def test_bedrock_reranker_maps_results() -> None:
    client = _client()
    docs = ["a", "b", "c"]
    with Stubber(client) as stub:
        stub.add_response(
            "rerank",
            {"results": [{"index": 0, "relevanceScore": 0.1}, {"index": 2, "relevanceScore": 0.9}]},
            {
                "queries": [{"type": "TEXT", "textQuery": {"text": "q"}}],
                "sources": [
                    {
                        "type": "INLINE",
                        "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d}},
                    }
                    for d in docs
                ],
                "rerankingConfiguration": {
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "modelConfiguration": {"modelArn": ARN},
                        "numberOfResults": 2,
                    },
                },
            },
        )
        hits = await BedrockReranker(client, ARN).rerank("q", docs, 2)
    assert [(h.index, h.relevance) for h in hits] == [(2, 0.9), (0, 0.1)]


async def test_bedrock_reranker_empty_docs_no_call() -> None:
    client = _client()
    with Stubber(client):  # any call would raise: no responses queued
        assert await BedrockReranker(client, ARN).rerank("q", [], 3) == []
