"""Rerankers: a no-op default and Amazon Rerank via Bedrock Agent Runtime."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import ProviderError
from app.llm.errors import log_provider_failure
from app.observability.usage import record_cost


@dataclass(frozen=True)
class RerankHit:
    index: int
    relevance: float | None


class Reranker(Protocol):
    async def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]: ...


class NoopReranker:
    async def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        return [RerankHit(i, None) for i in range(min(top_n, len(documents)))]


class BedrockReranker:
    def __init__(self, client: Any, model_arn: str) -> None:
        self._client = client
        self._model_arn = model_arn

    async def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        if not documents:
            return []

        def call() -> list[RerankHit]:
            try:
                resp = self._client.rerank(
                    queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                    sources=[
                        {
                            "type": "INLINE",
                            "inlineDocumentSource": {
                                "type": "TEXT",
                                "textDocument": {"text": d},
                            },
                        }
                        for d in documents
                    ],
                    rerankingConfiguration={
                        "type": "BEDROCK_RERANKING_MODEL",
                        "bedrockRerankingConfiguration": {
                            "modelConfiguration": {"modelArn": self._model_arn},
                            "numberOfResults": min(top_n, len(documents)),
                        },
                    },
                )
                hits = [RerankHit(r["index"], r["relevanceScore"]) for r in resp["results"]]
            except (ClientError, BotoCoreError, KeyError, TypeError) as exc:
                log_provider_failure("bedrock", exc)
                raise ProviderError("bedrock") from None
            record_cost("bedrock:rerank", lambda s: s.bedrock_rerank_usd_per_query)
            return sorted(hits, key=lambda h: h.relevance or 0.0, reverse=True)

        return await asyncio.to_thread(call)
