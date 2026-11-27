"""Embedders: offline hashing-trick default and Amazon Titan via Bedrock."""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import ProviderError
from app.llm.errors import log_provider_failure
from app.observability.usage import record_cost

_TOKEN = re.compile(r"[a-z0-9+#.]+")
_MAX_CONCURRENCY = 4


class Embedder(Protocol):
    dimensions: int

    @property
    def embed_model(self) -> str:
        """``provider:model:dims``; vectors from different identifiers are not comparable."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Deterministic bag-of-words hashing trick; texts sharing words have cosine > 0."""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    @property
    def embed_model(self) -> str:
        return f"hash:v1:{self.dimensions}"

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for tok in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            vec[h % self.dimensions] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class BedrockTitanEmbedder:
    def __init__(self, client: Any, model_id: str, dimensions: int) -> None:
        self._client = client
        self._model_id = model_id
        self.dimensions = dimensions

    @property
    def embed_model(self) -> str:
        return f"bedrock:{self._model_id}:{self.dimensions}"

    def _invoke(self, text: str) -> list[float]:
        try:
            resp = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps(
                    {"inputText": text, "dimensions": self.dimensions, "normalize": True}
                ),
                contentType="application/json",
                accept="application/json",
            )
            body = json.loads(resp["body"].read())
            embedding: list[float] = body["embedding"]
            if len(embedding) != self.dimensions:
                raise ValueError("unexpected embedding length")
        except (ClientError, BotoCoreError, KeyError, TypeError, ValueError) as exc:
            log_provider_failure("bedrock", exc)
            raise ProviderError("bedrock") from None
        tokens = body.get("inputTextTokenCount")
        if isinstance(tokens, int):
            record_cost(
                "bedrock:titan", lambda s: tokens * s.bedrock_embed_usd_per_mtok / 1_000_000
            )
        return embedding

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def one(t: str) -> list[float]:
            async with sem:
                return await asyncio.to_thread(self._invoke, t)

        return list(await asyncio.gather(*(one(t) for t in texts)))

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]
