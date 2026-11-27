"""Provider wiring. Bedrock clients are created lazily, only when selected."""

from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.config import Config
from langchain_core.language_models import BaseChatModel

from app.core.settings import get_settings
from app.llm.chat import make_chat_model
from app.llm.embeddings import BedrockTitanEmbedder, Embedder, HashEmbedder
from app.llm.rerank import BedrockReranker, NoopReranker, Reranker


@dataclass(frozen=True)
class Providers:
    embedder: Embedder
    reranker: Reranker
    chat_model: BaseChatModel


@lru_cache
def get_providers() -> Providers:
    s = get_settings()
    config = Config(
        region_name=s.aws_region,
        read_timeout=s.bedrock_timeout_s,
        connect_timeout=3,
        retries={"mode": "adaptive", "max_attempts": 4},
    )
    embedder: Embedder
    if s.embedder_provider == "bedrock":
        client = boto3.client("bedrock-runtime", config=config)
        embedder = BedrockTitanEmbedder(client, s.embed_model_id, s.embed_dimensions)
    else:
        embedder = HashEmbedder(s.embed_dimensions)
    reranker: Reranker
    if s.reranker_provider == "bedrock":
        agent_client = boto3.client("bedrock-agent-runtime", config=config)
        reranker = BedrockReranker(agent_client, s.effective_rerank_arn)
    else:
        reranker = NoopReranker()
    return Providers(embedder, reranker, make_chat_model(s))
