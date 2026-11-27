# Phase 5: Observability, Cost and Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every API request records its tokens, cost, cache use, latency and guardrail outcomes; a global daily spend cap backs up the per-user allowances; chat answers are served from a semantic cache when the question repeats; provider failures fall back to the other model; Langfuse traces graph runs with PII masked; `make report` prints p50/p95 latency and cost per route.

**Architecture:** A per-request `RequestUsage` ledger lives in a contextvar opened by a pure-ASGI `RequestLogMiddleware`. A LangChain callback attached to every chat model adds token usage to it; the Bedrock clients add their own units. When the response finishes, the middleware writes one `request_log` row. The spend cap sums today's `request_log.cost_usd`. The semantic cache is a pgvector table keyed by user + document-set hash and checked by a new chat-graph node. Langfuse is an optional LangChain callback passed in each graph's run config.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, langchain-anthropic 1.7.13 / anthropic 2.14, SQLAlchemy async + pgvector, Alembic, `langfuse>=5.2` (new), pytest.

**Spec:** `docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md` (§2 spend cap, §7.1 model layer, §7.6 guardrail events, §7.7 observability and cost, §7.8 secrets, §12 phase 5).

## Global Constraints

- Default model `claude-haiku-5-5` ($0.10 / $0.50 per MTok in/out), strong `claude-sonnet-5-5` ($2 / $10). Cache reads: Haiku $0.01, Sonnet $0.10 per MTok. Cache writes (5-minute TTL): 1.25× input ($0.125 / $2.50).
- Bedrock prices (config defaults, list price; re-check in Phase 7): Titan Text Embeddings V2 $0.02 per MTok; Amazon Rerank 1.0 $1.00 per 1,000 queries; Guardrails prompt-attack + PII $0.25 per 1,000 text units (1 unit = up to 1,000 characters).
- Global daily model-spend cap: `DAILY_SPEND_CAP_USD`, default `1.0`. Message: `The demo is at capacity today. Please try again tomorrow.` (HTTP 503, code `demo_at_capacity`).
- Semantic cache: cosine ≥ `0.95` (config), same documents, TTL 72h; applies to chat; bypassed when any guardrail outcome is not `allow`. Analyses keep their exact per-documents cache, which now also counts as a cache hit.
- Retries happen only on retryable errors (429, 5xx/overloaded, connection/timeout), never on 400/401/404. On exhaustion, fall back Haiku→Sonnet and Sonnet→Haiku. Retryable set: `anthropic.RateLimitError`, `anthropic.InternalServerError` (covers overloaded 529, 503, 504), `anthropic.APIConnectionError` (covers timeouts).
- No PII in logs, `request_log`, or Langfuse. Traces use the user id (never an email). Every string sent to Langfuse goes through `app.core.pii.redact_pii` (broad scope).
- All secrets come from Secrets Manager (`jobmatch/dev`, `jobmatch/prod`); new keys `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. Nothing secret goes in the repo, the image, or `.env.example`. Langfuse is off unless both keys are set.
- Unit tests stay offline: no Langfuse, Anthropic or AWS network calls. The fake LLM provider stays the default.
- Observability never fails a request: errors writing `request_log`, a Langfuse failure, or an unpriced model are logged with error type only and the response proceeds.
- ruff, `ruff format`, and `mypy --strict` stay clean. Commands run from `backend/` with `export PATH="$HOME/.local/bin:$PATH"`.

## Review Focus

1. A client disconnects mid-SSE stream → the `request_log` row is still written (shielded, bounded by a 5 s timeout) with whatever usage was recorded so far. Test in Task 2.
2. A model id missing from the price table (e.g. an env override) → cost 0, one `unpriced_model` warning per model per process, request succeeds. Test in Task 1.
3. A user uploads a new resume, then asks a question answered from the cache before → miss (the document-set key changed and the user's cache rows were deleted at ingestion). Test in Task 6.
4. Two users ask the identical question over identical sample documents → never served each other's cached answer. Test in Task 6.
5. Langfuse keys set but the host unreachable, or the handler raising → the chat still streams and completes. Test in Task 5.

---

### Task 1: Pricing, the per-request usage ledger, and the usage callback

**Files:**
- Create: `backend/app/llm/pricing.py`, `backend/app/observability/__init__.py`, `backend/app/observability/usage.py`
- Modify: `backend/app/core/settings.py` (price fields), `backend/app/llm/chat.py` (attach callback), `backend/app/llm/embeddings.py`, `backend/app/llm/rerank.py`, `backend/app/guardrails/bedrock.py`, `backend/app/guardrails/pipeline.py` (`record_outcome` notes non-allow outcomes)
- Test: `backend/tests/unit/test_usage.py`

**Interfaces:**
- Produces:
  - `app.llm.pricing.ModelPrice(BaseModel)`: `input`, `output`, `cache_read`, `cache_write` (USD per MTok, floats).
  - `app.llm.pricing.llm_cost_usd(prices: Mapping[str, ModelPrice], model: str, *, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float`. `input_tokens` is LangChain's total, which **includes** cache reads and writes, so uncached = `input_tokens - cache_read - cache_write`. An unknown model returns `0.0` and logs `unpriced_model` (with field `model`) once per model per process.
  - Settings: `model_prices: dict[str, ModelPrice]` with the defaults for both Claude models from Global Constraints; `bedrock_embed_usd_per_mtok: float = 0.02`; `bedrock_rerank_usd_per_query: float = 0.001`; `bedrock_guardrail_usd_per_unit: float = 0.00025`.
  - `app.observability.usage.RequestUsage`: a dataclass guarded by a `threading.Lock`. Fields: `user_id: UUID | None`, `models: list[str]` (distinct, first-seen order), `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` (ints), `cost_usd: float`, `cache_hit: bool`, `guardrails: list[str]` (`"<guardrail>:<action>"`). Methods `add_llm(model, *, input_tokens, output_tokens, cache_read, cache_write, cost_usd)` and `add_cost(source: str, cost_usd: float)` (Bedrock; `source` like `"bedrock:titan"` is appended to `models` once).
  - `current_usage() -> RequestUsage | None` (contextvar read) and `usage_scope() -> ContextManager[RequestUsage]` (sets a fresh ledger, resets on exit). With no scope open (scripts, unit tests), every recorder below is a no-op.
  - `UsageCallback(AsyncCallbackHandler)` with `on_llm_end`. For each generation whose `message.usage_metadata` is set, it reads `input_tokens`, `output_tokens` and `input_token_details` (`cache_read`, `cache_creation`). The model comes from `message.response_metadata["model_name"]`, else `"unknown"`. It prices the call with `get_settings().model_prices` and calls `add_llm`. A module-level `USAGE_CALLBACK` instance is passed as `callbacks=[USAGE_CALLBACK]` to every `ChatAnthropic` that `make_chat_model` builds.
- Bedrock: `BedrockTitanEmbedder._invoke` adds `inputTextTokenCount × price / 1e6` from the response body. `BedrockReranker.rerank` adds one query's price per call. `BedrockGuard` adds `ceil(len(text) / 1000)` units per text it sends. Each reads its price from `get_settings()`.
- `record_outcome` (pipeline): when `outcome.action != "allow"` and a ledger is open, it appends `f"{outcome.guardrail}:{outcome.action}"`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_usage.py`:
  - `test_llm_cost_haiku_with_cache`: `llm_cost_usd(defaults, "claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000, cache_read=400_000, cache_write=100_000)` ≈ `0.10*0.5 + 0.01*0.4 + 0.125*0.1 + 0.50` = `0.5665` (`pytest.approx`).
  - `test_unknown_model_costs_zero_and_warns_once`: two calls for `"claude-new-9"` return `0.0`; `capture_logs` shows exactly one `unpriced_model` event.
  - `test_callback_records_usage_into_open_scope`: inside `usage_scope()`, `await USAGE_CALLBACK.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x", usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "input_token_details": {"cache_read": 50}}, response_metadata={"model_name": "claude-haiku-5-5"}))]]), run_id=uuid4())`. Afterwards the ledger has `models == ["claude-haiku-5-5"]`, input 100, output 20, cache_read 50, and `cost_usd > 0`.
  - `test_callback_without_scope_is_noop`: the same call outside any scope does not raise.
  - `test_add_cost_and_models_distinct`: `add_cost("bedrock:titan", 0.001)` twice gives `models == ["bedrock:titan"]` and cost `0.002`.
  - `test_titan_embedder_records_cost`: a fake client whose `invoke_model` body has `inputTextTokenCount: 1000` adds `0.00002` inside a scope.
  - `test_guardrail_outcomes_noted_in_ledger`: `record_outcome` with a `flag` outcome inside a scope appends `"topic:flag"`; an `allow` outcome adds nothing.
  - `test_make_chat_model_attaches_usage_callback`: with `LLM_PROVIDER=anthropic` and a dummy key, `USAGE_CALLBACK in model.callbacks`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_usage.py -q`. Expected: FAIL (import errors).
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** `uv run pytest tests/unit -q && uv run ruff check . && uv run mypy`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: per-request usage ledger, pricing table and usage callback`.

### Task 2: `request_log` table and the request log middleware

**Files:**
- Create: `backend/migrations/versions/0006_request_log.py`, `backend/app/observability/request_log.py`
- Modify: `backend/app/db/models.py` (`RequestLog`), `backend/app/main.py` (middleware order), `backend/app/api/deps.py` (`get_current_user_id` sets `usage.user_id`)
- Test: `backend/tests/unit/test_request_log_middleware.py`, `backend/tests/integration/test_request_log.py`

**Interfaces:**
- Consumes: `usage_scope`, `RequestUsage`, `request_id_var`.
- Produces:
  - Table `request_log`:
    - `id uuid pk`, `request_id text not null`, `user_id uuid null` (FK `users`, `ON DELETE SET NULL`)
    - `route text not null` (the ASGI path), `method text not null`, `status int not null`
    - `models text[] not null default '{}'`
    - `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` (`int not null default 0`)
    - `cost_usd numeric(12,6) not null default 0`, `cache_hit bool not null default false`, `latency_ms int not null`
    - `guardrails text[] not null default '{}'`
    - `created_at timestamptz not null default now()`, plus index `ix_request_log_created` on `created_at`.
  - `RequestLogEntry` (frozen dataclass mirroring the columns, minus `id` and `created_at`).
  - `RequestLogWriter` Protocol `async write(entry: RequestLogEntry) -> None`.
  - `DbRequestLogWriter`: resolves `get_sessionmaker()` lazily at write time and writes shielded with `anyio.move_on_after(5)`, the same pattern as `DbRecorder`. Any exception or timeout is logged as `request_log_write_failed` / `request_log_write_timeout` (error type only) and never raised.
  - `RequestLogMiddleware(app, writer: RequestLogWriter)`, pure ASGI. Only `http` scopes whose path starts with `/api/` are logged. It opens `usage_scope()` around the inner app and captures the status from `http.response.start`. Once the call returns or raises, it builds the entry (status `500` if an exception escaped before a response started), emits `log.info("request_completed", route=..., status=..., latency_ms=..., cost_usd=..., cache_hit=..., model_count=...)`, and awaits `writer.write` inside a shielded scope, re-raising any exception afterwards.
  - In `create_app`: `app.add_middleware(RequestLogMiddleware, writer=DbRequestLogWriter())` is added **before** `RequestIdMiddleware`. `add_middleware` stacks last-added outermost, so the request id is bound first.

- [ ] **Step 1: Write failing unit tests** using a `ListWriter` and a tiny Starlette app:
  - `test_logs_api_request_with_usage`: a route that calls `current_usage().add_llm(...)` and returns 200 gives one entry with status 200, the usage and the request id.
  - `test_skips_non_api_paths`: `/health` gives no entry.
  - `test_exception_logs_500_and_reraises`: a route that raises `RuntimeError` gives an entry with status 500 and the exception propagates.
  - `test_streaming_response_logged_after_body`: a `StreamingResponse` route whose generator adds cost on its last chunk logs that cost (proves the entry is built after the body finishes).
  - `test_client_disconnect_still_writes` (Review Focus 1): the generator is cancelled mid-stream (`anyio` cancel scope around the ASGI call) and the writer still receives exactly one entry.
  - `test_writer_failure_never_breaks_response`: a writer raising `RuntimeError` still gives 200, plus a `request_log_write_failed` log (call `DbRequestLogWriter` with a sessionmaker that raises).
- [ ] **Step 2: Write the failing integration test** `test_chat_request_writes_row`: a POST to `/api/stream/chat` through the app (fake providers) writes a `request_log` row with route `/api/stream/chat`, status 200, the dev user id, and `latency_ms >= 0`.
- [ ] **Step 3: Run** both test files. Expected: FAIL.
- [ ] **Step 4: Implement** the migration (raw SQL like 0005, with a downgrade), the model, the writer, the middleware, the `deps` change and the `main` wiring.
- [ ] **Step 5: Run** `uv run pytest -q` (unit) and `make test-integration` (from the repo root). Expected: pass.
- [ ] **Step 6: Commit** `feat: request_log table and per-request log middleware`.

### Task 3: Global daily spend cap

**Files:**
- Create: `backend/app/guardrails/spend.py`
- Modify: `backend/app/core/settings.py` (`daily_spend_cap_usd: float | None = 1.0`), `backend/app/guardrails/allowance.py` (`consume_or_refuse` checks the cap first)
- Test: `backend/tests/integration/test_spend_cap.py`

**Interfaces:**
- Consumes: the `request_log` table (Task 2), `record_outcome`, `DbRecorder`.
- Produces:
  - `spent_today(session: AsyncSession, *, now: datetime | None = None) -> float`: `SUM(cost_usd)` of rows with `created_at >= today 00:00 UTC`, or 0.
  - `class DemoAtCapacity(AppError)`: `("demo_at_capacity", "The demo is at capacity today. Please try again tomorrow.", 503)`.
  - `ensure_budget(sessionmaker, user_id: UUID, *, settings: Settings) -> None`. If the cap is `None` it does nothing. When `spent_today >= cap` it records a `GuardrailOutcome(guardrail="spend_cap", provider="self", stage="chat_input", action="block", mode="enforce", reason="spend_cap", latency_ms=0, items=[])` via `record_outcome(DbRecorder(...))`, then raises `DemoAtCapacity`. A cap of `0` refuses everything.
  - `consume_or_refuse` calls `ensure_budget` before `consume`, so a request refused at capacity uses no allowance.

- [ ] **Step 1: Write failing integration tests:**
  - `test_under_cap_allows`: rows totalling $0.50 with a cap of 1.0 → `consume_or_refuse` succeeds.
  - `test_at_cap_refuses_with_message_and_event`: rows totalling $1.00 → `DemoAtCapacity` with the exact message; one `guardrail_events` row with `guardrail='spend_cap'`; the user's `usage_counters` are unchanged.
  - `test_yesterdays_spend_ignored`: a $5 row dated yesterday 23:59 UTC → allowed.
  - `test_cap_none_disables`: with `daily_spend_cap_usd=None`, $100 of rows → allowed.
  - `test_chat_api_at_capacity_returns_503`: a POST to `/api/stream/chat` → 503 with body code `demo_at_capacity`.
  Each test inserts its `request_log` rows directly. Delete rows created today before each test: other integration tests write rows through the middleware.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make test-integration` and the unit tests. Expected: pass.
- [ ] **Step 5: Commit** `feat: global daily spend cap backstop`.

### Task 4: Model fallback and prompt caching for structured calls

**Files:**
- Modify: `backend/app/llm/chat.py`, `backend/app/llm/providers.py`, `backend/app/llm/structured.py`, `backend/app/analysis/analyst.py`, `backend/app/graphs/chat.py` (type only), `backend/app/api/chat.py` if types require
- Test: `backend/tests/unit/test_fallback.py`, extend `backend/tests/unit/test_structured.py`

**Interfaces:**
- Produces:
  - `app.llm.chat.RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)`.
  - `type ChatRunnable = Runnable[LanguageModelInput, BaseMessage]`.
  - `with_fallback(primary: BaseChatModel, fallback: BaseChatModel) -> ChatRunnable` returns `primary.with_fallbacks([fallback], exceptions_to_handle=RETRYABLE_ERRORS)`.
  - `Providers.chat_model: ChatRunnable`. When `llm_provider == "anthropic"` it is Haiku `with_fallback` Sonnet; with the fake provider it is the plain fake model. `build_chat_graph(chat_model: ChatRunnable, ...)`.
  - `run_structured`: on a `RETRYABLE_ERRORS` exception from model `i`, if the other model (`1 - i` for two models) has not yet been tried in this call, log `structured_fallback` (`step`, `from_index`, `error_type`) and continue with it. Otherwise log through `log_provider_failure` and raise `ProviderError` as today. Non-retryable `anthropic.APIError`/`ModelError` raises immediately, as today. Validation escalation is unchanged and only moves forward.
  - Prompt caching: `ClaudeAnalyst._run` builds the human message as two text blocks. The first is the formatted context, with `"cache_control": {"type": "ephemeral"}`; the second is the task. The system prompt plus context exceeds the 512-token minimum, so feedback retries and same-model attempts read the cache. Chat prompts are not cached: the system prompt is about 300 tokens (below the minimum) and the retrieved context changes with every question.

- [ ] **Step 1: Write failing tests:**
  - `test_chat_fallback_on_overloaded`: the primary is a `GenericFakeChatModel` subclass whose `_astream`/`_agenerate` raise `anthropic.InternalServerError` (build it with `httpx2.Response(529, request=httpx2.Request("POST", "https://x"))`, `body=None`, `message="overloaded"`); the fallback is a fake that answers `"ok"`. `astream` yields `"ok"`.
  - `test_chat_no_fallback_on_bad_request`: the primary raises `anthropic.BadRequestError` (400) → the error propagates and the fallback is never called.
  - `test_structured_falls_back_to_other_model_on_rate_limit`: the model at index 0 raises `RateLimitError` and the model at index 1 returns valid output → the result has `model_index == 1`.
  - `test_structured_sonnet_falls_back_to_haiku`: `start_index=1`; index 1 raises `APIConnectionError` → `model_index == 0`.
  - `test_structured_both_unavailable_raises_provider_error`: both raise `InternalServerError` → `ProviderError`.
  - `test_analyst_context_block_has_cache_control`: capture the messages passed to a fake model. The human message content is a list whose first block has `cache_control == {"type": "ephemeral"}`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** all unit tests, ruff and mypy. Expected: pass.
- [ ] **Step 5: Commit** `feat: Haiku/Sonnet fallback on retryable errors; cache structured-call context`.

### Task 5: Langfuse tracing with PII masking

**Files:**
- Create: `backend/app/observability/tracing.py`
- Modify: `backend/pyproject.toml` + `uv.lock` (`uv add "langfuse>=5.2"`), `backend/app/core/settings.py`, `backend/app/main.py` (lifespan init and flush), `backend/app/graphs/chat.py` (`chat_events` passes config), `backend/app/graphs/match.py` (`analysis_events`), `backend/app/graphs/suggest.py`, `.env.example` (key names only, empty values), `backend/scripts/gen_env.py` (only if it filters keys)
- Test: `backend/tests/unit/test_tracing.py`

**Interfaces:**
- Settings: `langfuse_public_key: SecretStr | None = None`, `langfuse_secret_key: SecretStr | None = None`, `langfuse_host: str = "https://cloud.langfuse.com"`. A validator rejects setting only one of the two keys (`"LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set together"`).
- Produces:
  - `mask_pii(*, data: Any, **_: Any) -> Any`: recursively redacts every `str` inside dicts, lists and tuples with `redact_pii(s)`. Other values are returned unchanged.
  - `init_tracing(settings: Settings) -> bool`: when both keys are set, builds one `Langfuse(public_key=..., secret_key=..., host=settings.langfuse_host, mask=mask_pii, environment=settings.env)` and returns True. Otherwise it returns False and tracing stays off. Construction errors are logged as `tracing_init_failed` with the error type and leave tracing off.
  - `trace_config(*, name: str, user_id: UUID, tags: Sequence[str]) -> RunnableConfig`: always returns `{"run_name": name, "metadata": {"langfuse_user_id": str(user_id), "langfuse_session_id": request_id_var.get(), "langfuse_tags": list(tags)}}`. When tracing is on, `"callbacks"` is `[CallbackHandler()]` (`from langfuse.langchain import CallbackHandler`).
  - `flush_tracing() -> None`: flushes when on and never raises. It is called on lifespan shutdown. Per-invocation flushing on Lambda is a Phase 7 item.
  - Wiring:
    - `chat_events` → `graph.astream(state, config=trace_config(name="chat", user_id=..., tags=["chat", f"prompt:chat_system:{prompt_version}"]), stream_mode=...)`.
    - `analysis_events` → name `"analysis"`, tags `["analysis", f"analyst:{analyst.name}"]`.
    - `suggest` → name `"suggestions"`, tags `["suggestions", f"analyst:{analyst.name}"]`.
    - Guard outcomes reach the trace as the outputs of the `input_guards` and `output_guards` node spans. No extra events are needed.

- [ ] **Step 1: Write failing tests:**
  - `test_mask_pii_redacts_nested_strings`: `mask_pii(data={"a": ["mail x@y.co", ("call 415-555-0132",)], "n": 3})` → `{"a": ["mail [EMAIL]", ("call [PHONE]",)], "n": 3}`.
  - `test_tracing_off_without_keys`: `init_tracing(settings)` is False and `trace_config(...)` has no `"callbacks"` key, but has `metadata.langfuse_user_id`.
  - `test_one_key_only_rejected`: `Settings(database_url=..., langfuse_public_key="pk")` raises `ValidationError`.
  - `test_trace_config_carries_request_id_and_tags`: with `request_id_var` set to `"rid12345"`, the session id is `"rid12345"` and the tags are preserved.
  - `test_handler_failure_does_not_break_chat` (Review Focus 5): monkeypatch tracing on with a fake handler whose `on_chain_start` raises. Run `chat_events` over a fake graph: the `done` event is still yielded.
  - `test_init_with_keys_passes_mask`: monkeypatch `tracing.Langfuse` with a recorder class, then `init_tracing(settings_with_keys)`. The recorded kwargs include `mask=mask_pii` and the host.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** (`uv add "langfuse>=5.2"` from `backend/`).
- [ ] **Step 4: Run** all unit tests, ruff and mypy, then `make test-integration`. Expected: pass.
- [ ] **Step 5: Commit** `feat: optional Langfuse tracing with PII masking`.

### Task 6: Semantic cache for chat

**Files:**
- Create: `backend/app/cache/__init__.py`, `backend/app/cache/semantic.py`, `backend/migrations/versions/0007_semantic_cache.py`
- Modify:
  - `backend/app/db/models.py` (`SemanticCacheEntry`)
  - `backend/app/core/settings.py`: `semantic_cache_enabled: bool = True`, `semantic_cache_threshold: float = 0.95`, `semantic_cache_ttl_hours: int = 72`
  - `backend/app/retrieval/service.py`:
    - `Retriever.embed_query(query) -> list[float]`
    - `Retriever.active_docs(user_id) -> list[UUID]`
    - `candidates(..., *, embedding: list[float] | None = None)`
    - a read-only `embed_model` property
  - `backend/app/graphs/chat.py` (`cache_lookup` and `cache_store` nodes; state fields)
  - `backend/app/schemas/chat.py` (`ChatDone.cached: bool = False`)
  - `backend/app/api/chat.py` (builds the cache)
  - `backend/app/ingestion/service.py`: `_store` deletes the user's cache rows in its transaction
  - `backend/app/graphs/match.py`: a cached analysis sets `current_usage().cache_hit = True`
- Test: `backend/tests/unit/test_chat_graph.py` (extend), `backend/tests/integration/test_semantic_cache.py`

**Interfaces:**
- Table `semantic_cache`:
  - `id uuid pk`, `user_id uuid not null` (FK `users`, `ON DELETE CASCADE`), `doc_set_hash text not null`
  - `embedding vector(1024) not null`, `payload jsonb not null`
  - `created_at timestamptz default now()`, `expires_at timestamptz not null`
  - index `(user_id, doc_set_hash)`. Rows are per user, so an exact scan within the filter is enough; no HNSW index.
- Produces:
  - `doc_set_key(doc_ids: Sequence[UUID], *, embed_model: str, prompt_version: str, model: str) -> str`: sha256 hex of `"|".join(sorted(str(d)) ) + "|" + embed_model + "|" + prompt_version + "|" + model`.
  - `CachedAnswer(BaseModel)`: `answer: str`, `citations: list[Citation]`, `invalid_citations: list[str]`.
  - `SemanticCache(sessionmaker, *, threshold: float, ttl: timedelta)`:
    - `async lookup(user_id, key, embedding) -> CachedAnswer | None`: nearest row by `embedding <=> :q` with `user_id`, `doc_set_hash` and `expires_at > now()`. It is a hit when `1 - distance >= threshold`.
    - `async store(user_id, key, embedding, answer: CachedAnswer) -> None`.
    - Failures in either are logged (`semantic_cache_error`, error type only) and treated as a miss or no-op.
  - `invalidate_user_cache(session, user_id) -> None` (a DELETE, called inside `_store`'s transaction).
  - Chat graph: `build_chat_graph(..., cache: SemanticCache | None = None, cache_model: str = "")`. New state fields: `query_embedding: list[float] | None`, `cache_key: str | None`, `cached: CachedAnswer | None`. Flow:
    - `input_guards → after_input → (refuse | cache_lookup)`.
    - `cache_lookup`:
      - It is skipped (a miss) when `cache is None` or any outcome so far has `action != "allow"`.
      - Otherwise it embeds the question once, gets the active doc ids and builds the key. With no docs it is a miss.
      - On a hit it emits the progress step `"cache_hit"` and one `token` event with the cached answer, sets `cached`, `answer` and `refused=False`, and marks the ledger `cache_hit`.
      - Edges: a hit goes to `finalize`; a miss goes to `retrieve`, which reuses `query_embedding`.
    - `output_guards → cache_store → finalize`. `cache_store` stores only when the cache is on, the answer is not refused, the verdict is grounded, and every outcome is `allow`.
    - `ChatProgress.step` gains the literal `"cache_hit"` if it is a Literal.
  - `chat_events`: when `final["cached"]` is set, the citations and invalid citations come from it, `ChatDone.cached=True`, and the verdict is `Verdict(grounded=True, action="allow", unsupported_claims=[], mode=None)`. `cache_store` needs the citations: it computes them with `extract_citations` itself.
  - Cache hits still count against the chat allowance (it is charged before the graph runs).
  - `api/chat.py` passes `SemanticCache(...)` when `settings.semantic_cache_enabled`, with `cache_model=settings.chat_model`.

- [ ] **Step 1: Write failing unit tests** (fake cache object, fake retriever):
  - `test_cache_hit_skips_retrieval_and_generation`: the fake cache returns a `CachedAnswer`. Retriever `candidates` and the model are never called; the events are `progress(checking_input)`, `progress(cache_hit)`, `token`, `verdict`, then `done` with `cached=True` and the cached citations.
  - `test_flagged_input_bypasses_cache`: a monitor-mode guard outcome with `action="flag"` → lookup is never called.
  - `test_grounded_answer_is_stored`: on a miss with a grounded answer, `store` is called once with the answer and its citations.
  - `test_ungrounded_or_refused_not_stored`: neither an enforce `flag` verdict nor a refusal calls `store`.
- [ ] **Step 2: Write failing integration tests** (real DB, hash embedder):
  - `test_same_question_hits_second_time`: two identical chat requests; the second `done` has `cached=True`, and its `request_log` row has `cache_hit = true`.
  - `test_new_upload_invalidates` (Review Focus 3): ask, upload a different JD, then ask again → `cached=False`.
  - `test_users_do_not_share_entries` (Review Focus 4): store for user A; `lookup` for user B with the same key and embedding → None.
  - `test_expired_entry_misses`: a row with `expires_at` in the past → None.
  - `test_threshold_respected`: with a stored embedding, a query embedding at cosine about 0.9 misses at threshold 0.95. Construct the vectors directly.
- [ ] **Step 3: Run** them. Expected: FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** unit and integration suites, ruff and mypy. Expected: pass.
- [ ] **Step 6: Commit** `feat: semantic cache for grounded chat answers`.

### Task 7: Latency and cost report, and README

**Files:**
- Create: `backend/scripts/report.py`
- Modify: `Makefile` (`report` target: `$(RUN) python -m scripts.report --days $${DAYS:-7}`; add to `.PHONY`), `README.md` (`## Phase 5: observability and cost`)
- Test: `backend/tests/unit/test_report.py`, `backend/tests/integration/test_report_sql.py`

**Interfaces:**
- Produces:
  - `RouteStats` dataclass: `route`, `requests: int`, `p50_ms: float`, `p95_ms: float`, `avg_cost_usd: float`, `total_cost_usd: float`, `cache_hit_rate: float`.
  - `GuardStats` dataclass: `guardrail`, `action`, `count`.
  - `async fetch_route_stats(session, *, since: datetime) -> list[RouteStats]`: one SQL query grouped by route using `percentile_cont(0.5)` and `percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)`, ordered by request count desc.
  - `async fetch_guard_stats(session, *, since) -> list[GuardStats]`: grouped by guardrail and action.
  - `render_report(routes, guards, *, spent_today: float, cap: float | None, days: int) -> str`: Markdown with a header line `JobMatch report — last {days} day(s)`, a spend line `Spend today: $X.XXXX of $Y.YY cap` (or `(no cap)`), a route table with columns `route | requests | p50 ms | p95 ms | avg $ | total $ | cache hit %`, and a guardrail table. With no data it prints `No requests recorded.`.
  - `main()` parses `--days` and uses `get_settings()` and `get_sessionmaker()`.
- README section: what is recorded per request (no text, no PII), how to enable Langfuse (put the two keys in the `jobmatch/dev` secret, then `make env`), how cost is computed and where the prices live, the spend cap and its message, the semantic cache rules (threshold, TTL, bypass, invalidation, cache hits still use allowance), fallback behavior, and the prompt-caching scope (structured analysis retries only; prompts under 512 tokens don't cache). It also shows `make report` with sample output and gives the known limits: in-flight spend isn't counted by the cap; Langfuse flushes on shutdown only until Phase 7; Presidio masking adds latency to traced requests.

- [ ] **Step 1: Write failing tests:**
  - `test_render_report_tables`: one route and one guard row → contains `| /api/stream/chat | 3 |` and `Spend today: $0.0123 of $1.00 cap`.
  - `test_render_report_empty`: contains `No requests recorded.`.
  - Integration `test_fetch_route_stats_percentiles`: insert latencies 100/200/300/400 for one route; p50 is about 250 and p95 about 385; cache hit rate is correct.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the script, the Makefile target and the README section.
- [ ] **Step 4: Run** the unit and integration suites, then run `make report` against the Compose DB. Expected: a table prints.
- [ ] **Step 5: Commit** `feat: make report with p50/p95 latency and cost per route; docs`.
