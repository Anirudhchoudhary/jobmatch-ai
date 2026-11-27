"""Provider wiring. Bedrock clients are created lazily, only when selected."""

from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.config import Config
from langchain_core.language_models import BaseChatModel

from app.analysis.analyst import Analyst, ClaudeAnalyst
from app.analysis.heuristic import HeuristicAnalyst
from app.core.settings import get_settings
from app.llm.chat import ChatRunnable, make_chat_model, with_fallback
from app.llm.embeddings import BedrockTitanEmbedder, Embedder, HashEmbedder
from app.llm.rerank import BedrockReranker, NoopReranker, Reranker


@dataclass(frozen=True)
class Providers:
    embedder: Embedder
    reranker: Reranker
    chat_model: ChatRunnable
    structured_models: tuple[BaseChatModel, BaseChatModel]
    analyst: Analyst


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
    tokens, timeout = s.structured_max_tokens, s.structured_timeout_s
    structured = (
        make_chat_model(s, model=s.chat_model, max_tokens=tokens, timeout_s=timeout),
        make_chat_model(s, model=s.strong_chat_model, max_tokens=tokens, timeout_s=timeout),
    )
    analyst: Analyst
    if s.llm_provider == "anthropic":
        analyst = ClaudeAnalyst(
            structured,
            max_attempts=s.structured_max_attempts,
            strong_input_chars=s.strong_input_chars,
        )
    else:
        analyst = HeuristicAnalyst()
    chat_model: ChatRunnable
    if s.llm_provider == "anthropic":
        chat_model = with_fallback(
            make_chat_model(s), make_chat_model(s, model=s.strong_chat_model)
        )
    else:
        chat_model = make_chat_model(s)
    return Providers(embedder, reranker, chat_model, structured, analyst)
