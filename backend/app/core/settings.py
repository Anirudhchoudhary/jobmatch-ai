"""Application settings; secrets come from env/.env or AWS Secrets Manager."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import load_secret
from app.db.models import EMBEDDING_DIM
from app.llm.pricing import ModelPrice

_REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = _REPO_ROOT / ".env"


_MIN_PROD_KEY_LEN = 32
GuardMode = Literal["off", "monitor", "enforce"]


class _Bootstrap(BaseSettings):
    """Reads only the fields needed to decide where the real settings come from."""

    model_config = SettingsConfigDict(
        case_sensitive=False, extra="ignore", hide_input_in_errors=True
    )

    aws_region: str = "us-west-2"
    secrets_source: Literal["env", "aws"] = "env"
    secret_id: str = "jobmatch/dev"  # noqa: S105  (an id, not a secret)


class Settings(_Bootstrap):
    env: Literal["local", "ci", "dev", "prod"] = "local"
    database_url: SecretStr
    log_level: str = "INFO"
    db_connect_timeout_s: float = 3.0

    # Providers (offline fakes by default)
    llm_provider: Literal["fake", "anthropic"] = "fake"
    embedder_provider: Literal["hash", "bedrock"] = "hash"
    reranker_provider: Literal["none", "bedrock"] = "none"

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    chat_model: str = "claude-haiku-5-5"
    chat_max_tokens: int = 1024
    strong_chat_model: str = "claude-sonnet-5-5"
    # Adaptive thinking counts against max_tokens, so structured calls need headroom.
    structured_max_tokens: int = 12_000
    structured_max_attempts: int = 2
    strong_input_chars: int = 24_000
    suggest_max_bullets: int = 5
    llm_timeout_s: float = 30.0
    structured_timeout_s: float = 120.0
    # SSE comment sent when a stream has been silent this long; CloudFront drops an origin
    # response after 60 s without bytes.
    sse_keepalive_s: float = 15.0

    # Bedrock
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dimensions: int = 1024
    rerank_model_arn: str | None = None
    bedrock_timeout_s: float = 10.0

    # Pricing (USD)
    model_prices: dict[str, ModelPrice] = {
        "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
        "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.10, cache_write=2.50),
    }
    bedrock_embed_usd_per_mtok: float = 0.02
    bedrock_rerank_usd_per_query: float = 0.001
    bedrock_guardrail_usd_per_unit: float = 0.00025

    # Retrieval
    retrieval_mode: Literal["vector", "hybrid"] = "hybrid"
    retrieval_k: int = 20
    rrf_k: int = 60
    rerank_top_n: int = 6
    min_relevance: float = 0.2

    # Semantic cache for grounded chat answers
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95
    semantic_cache_ttl_hours: int = 72

    # Uploads
    upload_dir: Path = _REPO_ROOT / "var" / "uploads"
    max_upload_bytes: int = 5_242_880
    max_pdf_pages: int = 10
    jd_min_chars: int = 50
    jd_max_chars: int = 20_000

    # Guardrails
    guard_injection_rules: GuardMode = "enforce"
    guard_injection_classifier: GuardMode = "enforce"
    guard_topic: GuardMode = "enforce"
    guard_pii: GuardMode = "enforce"
    guard_groundedness: GuardMode = "enforce"
    guard_bedrock: GuardMode = "off"
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str | None = None
    guard_max_tokens: int = 2_000
    guard_timeout_s: float = 20.0
    groundedness_min_overlap: float = 0.5
    pii_engine: Literal["regex", "presidio"] = "presidio"

    # Allowances (per user per UTC day) and burst limit
    allowance_analyses: int = 5
    allowance_uploads: int = 10
    allowance_chats: int = 30
    allowance_suggestions: int = 10
    burst_per_minute: int = 10

    # Langfuse tracing (off unless both keys are set)
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    # Langfuse's current docs call this LANGFUSE_BASE_URL; LANGFUSE_HOST still works.
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("langfuse_host", "langfuse_base_url"),
    )

    # Global backstop: refuse requests once today's logged model + Bedrock cost reaches this
    # (None = off)
    daily_spend_cap_usd: float | None = 1.0

    # Sign-in (email OTP) and session cookie
    session_signing_key: SecretStr | None = None
    otp_hmac_key: SecretStr | None = None
    resend_api_key: SecretStr | None = None
    email_from: str | None = None
    email_sender: Literal["console", "resend"] = "console"
    session_cookie_name: str = "jm_session"
    session_ttl_days: int = 7
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_per_email_per_hour: int = 3
    otp_per_ip_per_hour: int = 10
    # All sign-in emails in a rolling 24 hours, kept under Resend's free-tier daily quota
    otp_global_per_day: int = 90
    # Proxies in front of the app that append to X-Forwarded-For (Lambda Function URL = 1);
    # only used when the Lambda request context carries no source IP
    trusted_proxy_hops: int = Field(default=1, ge=1)
    # Shared secret CloudFront sends as X-Origin-Verify; /api/* is refused without it (None = off)
    origin_verify_token: SecretStr | None = None

    # Deployment: blob storage and the retention cleanup job
    blob_backend: Literal["local", "s3"] = "local"
    uploads_bucket: str | None = None
    cleanup_mode: bool = False
    retention_hours: int = 72

    @model_validator(mode="after")
    def _bedrock_guard_needs_numbered_version(self) -> "Settings":
        if self.guard_bedrock != "off" and (
            not self.bedrock_guardrail_id
            or not self.bedrock_guardrail_version
            or self.bedrock_guardrail_version.upper() == "DRAFT"
        ):
            raise ValueError(
                "BEDROCK_GUARDRAIL_ID and a numbered BEDROCK_GUARDRAIL_VERSION "
                "are required when GUARD_BEDROCK is on"
            )
        return self

    @model_validator(mode="after")
    def _langfuse_keys_together(self) -> "Settings":
        if (self.langfuse_public_key is None) != (self.langfuse_secret_key is None):
            raise ValueError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set together")
        return self

    @model_validator(mode="after")
    def _require_anthropic_key(self) -> "Settings":
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        return self

    @model_validator(mode="after")
    def _embed_dimensions_match_schema(self) -> "Settings":
        if self.embed_dimensions != EMBEDDING_DIM:
            raise ValueError(
                f"EMBED_DIMENSIONS must be {EMBEDDING_DIM} to match the chunks.embedding column"
            )
        return self

    @model_validator(mode="after")
    def _prod_requires_auth_config(self) -> "Settings":
        if self.env == "prod":
            for name, key in (
                ("SESSION_SIGNING_KEY", self.session_signing_key),
                ("OTP_HMAC_KEY", self.otp_hmac_key),
            ):
                if key is None:
                    raise ValueError(f"{name} is required when ENV=prod")
                if len(key.get_secret_value()) < _MIN_PROD_KEY_LEN:
                    raise ValueError(
                        f"{name} must be at least {_MIN_PROD_KEY_LEN} characters when ENV=prod"
                    )
            if self.email_sender == "console":
                raise ValueError("EMAIL_SENDER=console is not allowed when ENV=prod")
        if self.email_sender == "resend" and (self.resend_api_key is None or not self.email_from):
            raise ValueError("RESEND_API_KEY and EMAIL_FROM are required when EMAIL_SENDER=resend")
        return self

    @model_validator(mode="after")
    def _origin_verify_token_strength(self) -> "Settings":
        if self.origin_verify_token is not None:
            length = len(self.origin_verify_token.get_secret_value())
            if length == 0:
                raise ValueError("ORIGIN_VERIFY_TOKEN must not be empty (unset it to disable)")
            if self.env == "prod" and length < _MIN_PROD_KEY_LEN:
                raise ValueError(
                    f"ORIGIN_VERIFY_TOKEN must be at least {_MIN_PROD_KEY_LEN} characters "
                    "when ENV=prod"
                )
        return self

    @model_validator(mode="after")
    def _s3_backend_needs_bucket(self) -> "Settings":
        if self.blob_backend == "s3" and not self.uploads_bucket:
            raise ValueError("UPLOADS_BUCKET is required when BLOB_BACKEND=s3")
        return self

    @property
    def effective_rerank_arn(self) -> str:
        return (
            self.rerank_model_arn
            or f"arn:aws:bedrock:{self.aws_region}::foundation-model/amazon.rerank-v1:0"
        )


@lru_cache
def get_settings() -> Settings:
    bootstrap = _Bootstrap(_env_file=ENV_FILE)
    overrides: dict[str, Any] = {}
    if bootstrap.secrets_source == "aws":
        secret = load_secret(bootstrap.secret_id, region=bootstrap.aws_region)
        overrides = {k.lower(): v for k, v in secret.items()}
    return Settings(_env_file=ENV_FILE, **overrides)
