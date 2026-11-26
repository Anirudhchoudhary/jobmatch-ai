# Phase 3: Structured Match Analysis and Resume Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given the user's ready resume and JD, produce:
- A grounded `MatchAnalysis` over SSE (`POST /api/stream/analyses`). It has a deterministic 0–100 score, matched, partial and missing skills, and verbatim evidence quotes from the resume.
- `BulletSuggestions` over JSON (`POST /api/suggestions`). These are rewritten resume bullets that never introduce skills, employers or metrics the source bullet lacks.

**Architecture:**
- **Graphs.** Two LangGraph graphs:
  - match: `load → extract_jd_requirements → extract_resume_skills → match_with_evidence → validate → score → finalize`;
  - suggest: `load → select_relevant_bullets → rewrite → validate → finalize`.
- **The `Analyst` interface.** Graph nodes call it, and it has two implementations:
  - `ClaudeAnalyst` (prompts in `backend/prompts/`, structured JSON-schema output through a retry-and-escalate runner);
  - `HeuristicAnalyst` (offline, deterministic, the default with `LLM_PROVIDER=fake`).
- **Deterministic steps.** Scoring and both validation steps (quote grounding, the unsupported-term check) are pure functions. A model can't choose the score or slip in invented evidence.
- **Caching.** Analyses are stored per (resume doc, JD doc, analyst) and returned from cache on repeat. Replacing a document cascades and deletes its analyses.

**Tech Stack:** The Phase 2 stack, with no new runtime dependencies. Structured output uses `langchain-anthropic`'s `with_structured_output(..., method="json_schema", include_raw=True)`.

**Spec:** `docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md`. Relevant sections: §6.1 flows 2–3, §7.1 (models, router, structured gotcha), §7.4 match and suggestion graphs, §7.5 structured outputs, §7.6 output rows (schema validation, hallucination block), §7.9, and §12 Phase 3. Phase 2's plan, `docs/superpowers/plans/2026-11-26-phase-2-ingestion-rag.md`, shows the existing conventions.

**Branch:** `feat/phase-3-match-suggest`, cut from `feat/phase-2-ingestion-rag` (PR #2 is not merged; this PR stacks on it).

## Global Constraints

- **Phase 1–2 rules still hold.**
  - `mypy --strict` passes; ruff is clean; secrets come only from Secrets Manager or the generated `.env`; no PII, document text or model output appears in logs.
  - Unit and integration tests are fully offline (fakes only) and print no warnings.
  - Every query on documents, chunks or analyses filters by `user_id`.
- **Models:**
  - Default: `claude-haiku-5-5`. Strong: `claude-sonnet-5-5` (setting `strong_chat_model`).
  - Every structured call uses `with_structured_output(schema, method="json_schema", include_raw=True)`. Never use forced `tool_choice`, because Sonnet 5.5 rejects it.
  - Structured calls use `max_tokens=4096` (setting `structured_max_tokens`) and effort `medium`.
- **Retry and escalation (spec §7.1/§7.5):**
  - Each model in the ladder `[default, strong]` gets `structured_max_attempts=2` attempts.
  - The second attempt includes the validation errors as feedback.
  - Then escalate to the strong model; after the last attempt, raise `AppError("model_output_invalid", "The model returned an unusable answer. Please try again.", 502)`.
  - Start directly on the strong model when the prompt is longer than `strong_input_chars=24_000` characters.
  - A provider error (`anthropic.APIError`/`ModelError`) is not retried here. It raises `ProviderError("anthropic")` (the fallback is Phase 5).
- **Score (deterministic, spec §7.4):**
  - Weights: `required` = 3, `nice_to_have` = 1. Credit: `matched` = 1.0, `partial` = 0.5, `missing` = 0.
  - `score = round(100 * Σ weight·credit / Σ weight)`.
  - Zero requirements raises `AppError("no_requirements", "We couldn't find any requirements in the job description.", 422)`.
- **Evidence grounding (hallucination block):**
  - An evidence quote is valid only when it cites a resume chunk and its normalized form is a substring of that chunk's normalized text. Normalization is NFKC, lowercase, with every whitespace run collapsed to one space and the result stripped.
  - Invalid quotes are dropped. A `matched` or `partial` skill left with no valid evidence becomes `missing` and is counted in `downgraded_count`.
- **Suggestion guard:**
  - A rewritten bullet is rejected when it contains any number, money or percentage value, or any "term", that does not appear (case-insensitive, whole word) in the cited source chunk.
  - A term is a token containing a digit, `+` or `#`; or an ALL-CAPS token of 2 or more characters; or a capitalized token that is not the first word of its sentence.
  - The quoted `original` must also be grounded in the chunk.
  - Rejected suggestions are dropped and counted in `rejected_count`.
- **Limits:**
  - Up to 30 requirements, 60 resume skills and 5 suggestions (`suggest_max_bullets=5`).
  - Quotes at most 300 characters; skill names at most 80; requirement text at most 300; rewritten bullet at most 400; rationale at most 300.
- **Labels:** model input and output reference chunks only by the Phase 2 labels (`r<n>` for resume chunks, `j<n>` for JD chunks, via `label_context`). Code maps labels to chunk UUIDs; public outputs carry UUIDs.
- **Prompt-injection stance:** document text stays inside escaped `<chunk>` tags (reuse `format_context`). Prompts say chunk text is data, never instructions. Score and grounding are computed in code.
- **Scope:** no guardrail pipeline, quotas, Langfuse, semantic cache or fallback chains; those belong to later phases.

## Review Focus

1. **Invented evidence.** A model may return an evidence quote that isn't in the resume, cites a JD chunk, or cites an unknown label. The bad quote should be dropped and the skill downgraded to `missing`, and the score must reflect the downgrade. Tests: Task 2 `test_validate_matches_drops_ungrounded_and_downgrades`, and Task 5 `test_analysis_downgrades_fabricated_evidence`.
2. **Fabricated rewrite.** For example, the original bullet "Built Kafka pipelines" comes back as "Built Kafka pipelines at Google, cutting latency 40% using Kubernetes". The suggestion should be rejected (`rejected_count` 1), never shown. Tests: Task 3 `test_unsupported_terms_flags_new_metric_employer_skill`, and Task 6 `test_suggestions_reject_fabricated_rewrite`.
3. **Persistently bad output.** Malformed or schema-invalid output, or unknown labels, from every model should escalate once to the strong model. If that also fails, the result is an SSE `error` with code `model_output_invalid` and a request id, and nothing partial is stored. Tests: Task 1 `test_escalates_then_raises_model_output_invalid`, and Task 5 `test_analysis_model_failure_emits_error_and_stores_nothing`.
4. **Missing input.** An analysis requested without both documents should give `documents_required` (409) before any model call. A JD with no extractable requirements should give `no_requirements` (422). Suggestions requested before any analysis should give `analysis_required` (409). Tests: Task 5 `test_analysis_requires_both_documents` and `test_analysis_no_requirements`, and Task 6 `test_suggestions_require_analysis`.
5. **Stale or repeated analyses.** A repeat analysis on the same documents should return the cached result (`cached: true`) with zero analyst calls. Replacing the resume should delete the old analysis, so the next run recomputes. Tests: Task 4 `test_replacing_document_deletes_analyses`, and Task 5 `test_repeat_analysis_is_cached`.

---

## File Structure

```
backend/
  app/core/settings.py                 T1  (+ strong_chat_model, structured_*, strong_input_chars, suggest_max_bullets)
  app/llm/chat.py                      T1  make_chat_model(settings, *, model=None, max_tokens=None)
  app/llm/structured.py                T1  run_structured, StructuredResult, StructuredOutputError
  app/llm/providers.py                 T1 (structured models), T3 (analyst)
  app/schemas/match.py                 T2  public + draft schemas
  app/analysis/__init__.py             T2
  app/analysis/score.py                T2  compute_score
  app/analysis/grounding.py            T2 (normalize, quote_in_chunk, validate_matches), T3 (unsupported_terms, validate_suggestions)
  app/analysis/analyst.py              T3  Analyst protocol, ClaudeAnalyst, label validators
  app/analysis/heuristic.py            T3  HeuristicAnalyst
  prompts/{extract_requirements,extract_skills,match_skills,rewrite_bullets}.md   T3
  migrations/versions/0004_analyses.py T4
  app/db/models.py                     T4  Analysis
  app/analysis/repo.py                 T4  load_documents, get_cached_analysis, save_analysis, latest_analysis
  app/graphs/match.py                  T5
  app/api/analyses.py                  T5
  app/graphs/suggest.py                T6
  app/api/suggestions.py               T6
  tests/fixtures/structured.py         T1  ScriptedStructuredModel (test fake)
README.md                              T7
```

---

### Task 1: Structured-output runner with retry and escalation

**Files:**
- Create: `app/llm/structured.py`, `tests/fixtures/structured.py`, `tests/unit/test_structured.py`
- Modify: `app/core/settings.py`, `app/llm/chat.py`, `app/llm/providers.py`, `tests/unit/test_providers.py`

**Interfaces:**
- Consumes: `ProviderError`, `log_provider_failure` (`app/llm/errors.py`), `AppError`.
- Produces:
  - **Settings:** `strong_chat_model: str = "claude-sonnet-5-5"`, `structured_max_tokens: int = 4096`, `structured_max_attempts: int = 2`, `strong_input_chars: int = 24_000`, `suggest_max_bullets: int = 5`.
  - **`make_chat_model(settings, *, model: str | None = None, max_tokens: int | None = None) -> BaseChatModel`:**
    - The defaults are unchanged (`chat_model`, `chat_max_tokens`).
    - The fake branch ignores both arguments.
  - **`Providers.structured_models: tuple[BaseChatModel, BaseChatModel]`:** `(default model at structured_max_tokens, strong model at structured_max_tokens)`. With the fake provider, these are two fake chat models.
  - **`class StructuredOutputError(AppError)`:** code `"model_output_invalid"`, status 502, message `"The model returned an unusable answer. Please try again."`.
  - **`StructuredResult[T]`** (frozen generic dataclass): `value: T`, `model_index: int`, `attempts: int`.
  - **`async def run_structured(schema: type[T], messages: Sequence[BaseMessage], *, models: Sequence[BaseChatModel], step: str, validate: Callable[[T], list[str]] = lambda _: [], max_attempts_per_model: int = 2, start_index: int = 0) -> StructuredResult[T]`:**
    - For each model starting at `start_index`, make up to `max_attempts_per_model` attempts.
    - Each attempt calls `model.with_structured_output(schema, method="json_schema", include_raw=True).ainvoke(msgs)`.
    - The attempt fails when `parsing_error` is set, `parsed` is None, or `validate(parsed)` returns errors.
    - On failure, the next attempt's messages are the originals plus `AIMessage(raw text)` plus a `HumanMessage` listing the errors (at most 10 lines, each at most 300 characters). Return only corrected JSON.
    - After every model is exhausted, raise `StructuredOutputError`.
    - `anthropic.APIError`/`ModelError` call `log_provider_failure` and raise `ProviderError("anthropic") from exc` immediately.
    - Logs carry `step`, `model_index`, `attempt`, `error_count` and `escalated`; never content or error text.
  - **`def pick_start_index(messages: Sequence[BaseMessage], threshold_chars: int) -> int`:** 1 if the total message content is longer than `threshold_chars`, else 0.
  - **Test fake `ScriptedStructuredModel(BaseChatModel)`** in `tests/fixtures/structured.py`:
    - `__init__(script: list[dict | Exception])`.
    - Its `with_structured_output` returns a runnable that pops the next scripted item.
    - A dict is treated as JSON and validated with `schema.model_validate`, producing `{"raw": AIMessage(json), "parsed": obj | None, "parsing_error": err | None}`.
    - An `Exception` item is raised.
    - It records `calls: list[list[BaseMessage]]`.

- [ ] **Step 1: Failing tests** in `test_structured.py`. Use a tiny schema `class Out(BaseModel): n: int`.
  - `test_first_attempt_success`: script `[{"n": 1}]` gives `value.n == 1`, `model_index == 0` and `attempts == 1`.
  - `test_retry_feeds_back_errors`: primary script `[{"n": "x"}, {"n": 2}]` gives `value.n == 2` and `attempts == 2`. The second call's last message is a `HumanMessage` containing `"invalid"`.
  - `test_semantic_validate_retries`: `validate=lambda o: [] if o.n > 0 else ["n must be positive"]` with script `[{"n": 0}, {"n": 3}]` gives 3, and the feedback contains `"n must be positive"`.
  - `test_escalates_then_raises_model_output_invalid`:
    - With both models always invalid (`[{"n": "x"}]*2` each), it raises `StructuredOutputError` with code `model_output_invalid` and status 502, after 2 calls on each model.
    - With the primary invalid twice and the strong model returning `{"n": 5}`, it gives `model_index == 1`.
  - `test_start_index_skips_primary`: with `start_index=1`, the primary has 0 calls.
  - `test_provider_error_not_retried`: script `[anthropic.APIConnectionError(request=httpx.Request("POST","https://x"))]` raises `ProviderError` after 1 call.
  - `test_pick_start_index`: a 30 000-character message gives 1, and a short one gives 0.
  - `test_logs_have_no_content`: with `capture_logs`, a run whose feedback contains `"SECRETQUOTE"` leaves the string absent from every log event.
- [ ] **Step 2: Failing test** `test_structured_models_ladder` in `test_providers.py`. With `LLM_PROVIDER=anthropic` and a fake key, `get_providers().structured_models` has `model` names `("claude-haiku-5-5", "claude-sonnet-5-5")` and `max_tokens == 4096` on both. With the fake provider, the result is a tuple of 2.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** Check in the installed `langchain-anthropic` source that `method="json_schema"` is supported and that `include_raw=True` returns the `raw`/`parsed`/`parsing_error` dict. Record the findings in the report.
- [ ] **Step 5: Run** `make lint typecheck test`. Expected: green.
- [ ] **Step 6: Commit** with message `feat: structured-output runner with feedback retry and model escalation`.

---

### Task 2: Match schemas, deterministic score, evidence grounding

**Files:**
- Create: `app/schemas/match.py`, `app/analysis/__init__.py`, `app/analysis/score.py`, `app/analysis/grounding.py`, `tests/unit/test_score.py`, `tests/unit/test_grounding.py`

**Interfaces:**
- Consumes: `RetrievedChunk` (`app/retrieval/search.py`).
- Produces, in `app/schemas/match.py`:
  - Shared aliases:
    - `Importance = Literal["required","nice_to_have"]`;
    - `MatchStatus = Literal["matched","partial","missing"]`;
    - `Label` is `str` constrained to `^[rj]\d+$`.
  - **Drafts** (model I/O, labels only):
    - `EvidenceDraft(source: Label, quote: str 1..300)`.
    - `RequirementDraft(skill: str 1..80, text: str 1..300, importance: Importance, source: Label)`, with `RequirementsOut(requirements: list[RequirementDraft], max 30)`.
    - `SkillDraft(skill: str 1..80, evidence: list[EvidenceDraft], max 5)`, with `SkillsOut(skills: list[SkillDraft], max 60)`.
    - `MatchDraft(requirement_index: int ≥0, status: MatchStatus, evidence: list[EvidenceDraft], max 5)`, with `MatchesOut(matches: list[MatchDraft])`.
    - `SuggestionDraft(source: Label, original: str 1..300, rewritten: str 1..400, rationale: str 1..300, target_skills: list[str], max 5)`, with `SuggestionsOut(suggestions: list[SuggestionDraft], max 5)`.
  - **Public** (UUIDs):
    - `Evidence(chunk_id: UUID, quote: str)`.
    - `Requirement(skill, text, importance, jd_chunk_id: UUID)`.
    - `SkillMatch(requirement: Requirement, status: MatchStatus, evidence: list[Evidence])`.
    - `MatchAnalysis`, with fields:
      - `analysis_id: UUID`, `score: int 0..100`;
      - `required_coverage: float`, `nice_to_have_coverage: float | None`;
      - `skills: list[SkillMatch]`, `downgraded_count: int`;
      - `analyst: str`, `cached: bool`, `request_id: str`.
    - `BulletSuggestion(original_chunk_id: UUID, original: str, rewritten: str, rationale: str, target_skills: list[str])`.
    - `BulletSuggestions(analysis_id: UUID, suggestions: list[BulletSuggestion], rejected_count: int, analyst: str, request_id: str)`.
    - `AnalysisProgress(step: Literal["loading","extracting_requirements","extracting_skills","matching","validating","scoring"])`.
    - `SuggestionRequest(analysis_id: UUID | None = None)`.
- Produces, in `app/analysis/score.py`:
  - **`ScoreResult`** (frozen dataclass): `score: int`, `required_coverage: float`, `nice_to_have_coverage: float | None`.
  - **`compute_score(skills: Sequence[SkillMatch]) -> ScoreResult`:**
    - Uses the Global Constraints weights and credits.
    - Coverage is Σcredit/count within each importance; it is None when that importance has no requirements.
    - Raises `no_requirements` on empty input.
- Produces, in `app/analysis/grounding.py`:
  - `normalize(text: str) -> str`, as defined in Global Constraints.
  - `quote_in_chunk(quote: str, chunk_text: str) -> bool`.
  - **`validate_matches(drafts: Sequence[MatchDraft], requirements: Sequence[Requirement], labelled: Mapping[str, RetrievedChunk]) -> tuple[list[SkillMatch], int]`:**
    - Keeps an evidence item only when its label is in `labelled`, points to a `resume` chunk, and `quote_in_chunk` holds.
    - A `matched`/`partial` draft with no surviving evidence becomes `missing`, and the downgrade is counted.
    - `missing` keeps empty evidence.
    - The output has one `SkillMatch` per requirement, in requirement order.
    - A requirement with no draft is `missing`, and is not counted as a downgrade.

- [ ] **Step 1: Failing tests** in `test_score.py`:
  - `test_score_weights`: 2 required (one matched, one missing) and 1 nice-to-have (partial) give `round(100*(3*1+3*0+1*0.5)/7) == 50`, `required_coverage == 0.5` and `nice_to_have_coverage == 0.5`.
  - `test_score_all_matched_100` and `test_score_all_missing_0`.
  - `test_only_required_has_none_nice`.
  - `test_empty_raises_no_requirements`: `AppError` with code `no_requirements` and status 422.
- [ ] **Step 2: Failing tests** in `test_grounding.py`. Use labelled fixtures: `r1` is `"Built Kafka   pipelines\nprocessing 2M events/day"` and `j1` is a JD chunk.
  - `test_quote_normalization`: `quote_in_chunk("built kafka pipelines processing", r1.text)` is True. `quote_in_chunk("built kafka streams", r1.text)` is False.
  - `test_validate_matches_drops_ungrounded_and_downgrades`. Four drafts:
    - matched with a valid r1 quote: stays `matched` with 1 evidence;
    - matched with an invented quote: becomes `missing`;
    - partial citing `j1`: becomes `missing`;
    - matched citing `r9` (unknown): becomes `missing`.
    - Expected `downgraded == 3`.
  - `test_validate_matches_fills_unmatched_requirements`: 2 requirements and 1 draft give 2 `SkillMatch`, with the second `missing` and a downgrade count of 0.
  - `test_label_pattern_rejected_by_schema`: `EvidenceDraft(source="x1", quote="q")` raises `ValidationError`.
- [ ] **Step 3: Run.** Expected: FAIL. **Step 4: Implement** as pure functions, with no I/O.
- [ ] **Step 5: Run** `make lint typecheck test`. **Step 6: Commit** with message `feat: match schemas, deterministic score, evidence grounding`.

---

### Task 3: Suggestion guard, `Analyst` interface, Claude and heuristic analysts

**Files:**
- Create: `app/analysis/analyst.py`, `app/analysis/heuristic.py`, `prompts/extract_requirements.md`, `prompts/extract_skills.md`, `prompts/match_skills.md`, `prompts/rewrite_bullets.md`, `tests/unit/test_suggestion_guard.py`, `tests/unit/test_analyst.py`, `tests/unit/test_heuristic.py`
- Modify: `app/analysis/grounding.py`, `app/llm/providers.py` (`Providers.analyst`)

**Interfaces:**
- Consumes:
  - From T1: `run_structured`, `pick_start_index`, `StructuredOutputError`.
  - From T2: the drafts and public schemas, `validate_matches`.
  - From Phase 2: `label_context` and `format_context` (`app/graphs/citations.py`), `load_prompt` (`app/llm/prompts.py`).
- Produces, in `grounding.py`:
  - **`unsupported_terms(rewritten: str, source: str) -> list[str]`:** sorted and unique. It flags numbers or money (`\$?\d[\d,.]*[%kKmMbB]?`, with commas removed before comparing) and "terms" (as defined in Global Constraints) that are not present case-insensitively as whole words in `source`.
  - **`validate_suggestions(drafts: Sequence[SuggestionDraft], labelled: Mapping[str, RetrievedChunk]) -> tuple[list[BulletSuggestion], int]`:**
    - Keeps a draft only when its label is a resume chunk, `quote_in_chunk(draft.original, chunk.text)` holds, and `unsupported_terms(draft.rewritten, chunk.text)` is empty.
    - Each dropped draft counts as a rejection.
- Produces, in `analyst.py`:
  - **`class Analyst(Protocol)`:**
    - `name: str`, a cache key; for example `"heuristic:v1"`, or `f"claude:{default}:{strong}:{sha8 of the four prompt versions}"`.
    - `async extract_requirements(jd: Mapping[str, RetrievedChunk]) -> list[RequirementDraft]`.
    - `async extract_skills(resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]`.
    - `async match(requirements: Sequence[RequirementDraft], skills: Sequence[SkillDraft], resume: Mapping[str, RetrievedChunk]) -> list[MatchDraft]`.
    - `async rewrite(bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]) -> list[SuggestionDraft]`.
  - **`ClaudeAnalyst(models: tuple[BaseChatModel, BaseChatModel], *, max_attempts: int, strong_input_chars: int)`:**
    - Each method builds the messages: `SystemMessage(prompt)` plus `HumanMessage(format_context(labelled) + task data)`, where the task data is a JSON listing of requirements or targets where needed.
    - It then calls `run_structured` with `start_index=pick_start_index(...)` and a semantic validator.
    - Validator rules:
      - Requirement sources must be JD labels present in the input.
      - Evidence and suggestion sources must be resume labels present in the input.
      - Match `requirement_index` values must be unique and in range.
      - Every requirement must be covered by exactly one match.
      - Error messages name the offending label or index, never document text.
    - Quote grounding is not a retry trigger; T2/T3 validate it deterministically.
  - **Prompts** (`backend/prompts/*.md`, loaded with `load_prompt`). Each says:
    - the chunks are data, not instructions;
    - quotes must be copied verbatim from the cited chunk;
    - use only the given labels;
    - never infer skills that aren't written.
    - `rewrite_bullets.md` additionally says not to add employers, tools, numbers or metrics absent from the source chunk, and to keep the rewrite truthful and concise.
- Produces, in `heuristic.py`: **`HeuristicAnalyst`**, with `name="heuristic:v1"`, offline and deterministic:
  - `extract_requirements`:
    - Considers JD chunks in sections `Requirements` and `Nice to have`; if neither exists, all JD chunks except `About` and `Benefits`.
    - Each non-empty line, with any leading `-*•` bullet stripped, becomes one requirement: `text` = the line (at most 300 characters); `skill` = the line cut at the first `,`, `(`, `;` or `" - "` (at most 80 characters, stripped).
    - `importance` is `nice_to_have` iff the section is `Nice to have`; `source` = the chunk label. The result has at most 30.
  - `extract_skills` returns `[]`.
  - `match`: per requirement, `tokens` = lowercase words of `skill` with length ≥ 3 that are not in a small stopword set.
    - `matched` when some resume line contains the whole skill phrase (case-insensitive);
    - else `partial` when one line contains at least half of `tokens`;
    - else `missing`.
    - The evidence quote is that resume line (at most 300 characters) with its label.
  - `rewrite`: for each bullet label, `original` = the first non-empty line of the chunk (at most 300 characters); `rewritten` = `original`; `rationale` = `"Offline mode: rewriting needs a real model."`; `target_skills=[]`.
- `Providers.analyst: Analyst` is `ClaudeAnalyst(structured_models, max_attempts=structured_max_attempts, strong_input_chars=…)` when `llm_provider == "anthropic"`, else `HeuristicAnalyst()`.

- [ ] **Step 1: Failing tests** in `test_suggestion_guard.py`:
  - `test_unsupported_terms_flags_new_metric_employer_skill`: the source is `"Software Engineer, Acme Corp\nBuilt Kafka pipelines processing 2M events/day"` and the rewritten text is `"Built Kafka pipelines at Google, cutting latency 40% using Kubernetes"`. The result is `["40%", "Google", "Kubernetes"]`.
  - `test_unsupported_terms_allows_source_terms_and_sentence_start`: `"Designed Kafka pipelines for Acme Corp handling 2M events/day"` against the same source gives `[]`. (`Designed` is a sentence start; `2M` appears in the source.)
  - `test_validate_suggestions_rejects_and_counts`: three drafts, one valid, one with an ungrounded `original`, and one citing `j1`. The result is 1 kept and `rejected == 2`.
- [ ] **Step 2: Failing tests** in `test_analyst.py`, using `ScriptedStructuredModel`:
  - `test_requirements_unknown_label_retried`: the first output cites `j9`, which is absent; the second is valid. The result is valid; the feedback mentions `j9` and contains no chunk text.
  - `test_match_must_cover_each_requirement_once`: the first output skips index 1 and the second covers both. The result covers both.
  - `test_messages_escape_and_label_context`: the user message contains `<chunk label="r1"` and escapes a `</chunk>` that appears in the chunk text.
  - `test_name_includes_models_and_prompt_versions`.
- [ ] **Step 3: Failing tests** in `test_heuristic.py`, against the Phase 2 sample JD/resume texts (`app/samples/*.md` chunked with `chunk_resume`/`chunk_jd`):
  - `test_heuristic_requirements_from_sections`: the result includes a Kubernetes requirement with importance `required`, and every nice-to-have item is `nice_to_have`.
  - `test_heuristic_match_kafka_matched_kubernetes_missing`.
  - `test_heuristic_rewrite_is_guard_clean`: `validate_suggestions(rewrite(...))` rejects nothing.
- [ ] **Step 4: Run.** Expected: FAIL. **Step 5: Implement.**
- [ ] **Step 6: Run** `make lint typecheck test`. **Step 7: Commit** with message `feat: analyst interface with Claude (structured) and offline heuristic implementations`.

---

### Task 4: Analyses table and repository

**Files:**
- Create: `migrations/versions/0004_analyses.py`, `app/analysis/repo.py`, `tests/integration/test_analysis_repo.py`
- Modify: `app/db/models.py`

**Interfaces:**
- Consumes: `Document`, `Chunk`, `RetrievedChunk`, `get_sessionmaker`, `MatchAnalysis`.
- Produces:
  - **`Analysis` model**, table `analyses`:
    - `id` UUID primary key;
    - `user_id` foreign key to users (cascade);
    - `resume_doc_id` and `jd_doc_id`, both foreign keys to documents with `ON DELETE CASCADE`;
    - `analyst` text; `result` JSONB (the `MatchAnalysis` dump, without `cached`/`request_id`); `created_at`.
    - Unique `uq_analyses_docs (user_id, resume_doc_id, jd_doc_id, analyst)`, and index `ix_analyses_user`.
    - Migration revision `"0004"`, down revision `"0003"`; the downgrade drops the table.
  - **`LoadedDocuments`** (frozen dataclass): `resume_doc_id: UUID`, `jd_doc_id: UUID`, `resume: list[RetrievedChunk]`, `jd: list[RetrievedChunk]`, each ordered by ordinal, with `score=0.0`.
  - **`async def load_documents(session, user_id) -> LoadedDocuments`:** loads the user's `ready` resume and JD and all their chunks. If either is missing it raises `AppError("documents_required", "Upload a resume and a job description first.", 409)`.
  - **`async def get_cached_analysis(session, user_id, resume_doc_id, jd_doc_id, analyst) -> tuple[UUID, dict] | None`.**
  - **`async def save_analysis(session, *, user_id, resume_doc_id, jd_doc_id, analyst, result: dict) -> UUID`:** `INSERT … ON CONFLICT (uq_analyses_docs) DO UPDATE SET result = EXCLUDED.result` returning the id, then commit.
  - **`async def get_analysis(session, user_id, analysis_id) -> tuple[UUID, UUID, dict] | None`:** returns `(resume_doc_id, jd_doc_id, result)`.
  - **`async def latest_analysis(session, user_id, resume_doc_id, jd_doc_id) -> tuple[UUID, dict] | None`:** the newest by `created_at`, any analyst.

- [ ] **Step 1: Failing integration tests.** Use per-test users and seed docs through `ingest_document` with `HashEmbedder`.
  - `test_load_documents_requires_both`: only a JD gives `documents_required` (409).
  - `test_load_documents_orders_chunks`: ordinals are increasing; there is a resume list and a JD list.
  - `test_save_get_roundtrip_and_upsert`: saving twice under the same key returns the same id with the updated result.
  - `test_cached_lookup_is_user_scoped`: user B can't read A's analysis, through either `get_analysis` or `get_cached_analysis`.
  - `test_replacing_document_deletes_analyses`: save an analysis, ingest a different resume, and the analysis row is gone.
  - `test_downgrade_upgrade_0004`.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run** `make lint typecheck test migrate test-integration`. **Step 5: Commit** with message `feat: analyses table with doc-cascade invalidation and repository`.

---

### Task 5: Match graph and `POST /api/stream/analyses`

**Files:**
- Create: `app/graphs/match.py`, `app/api/analyses.py`, `tests/unit/test_match_graph.py`, `tests/integration/test_analyses_api.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `Analyst` (T3), `validate_matches` (T2), `compute_score` (T2), repo (T4), `label_context`, `sse_response`, `request_id_var`, `get_current_user_id`, `get_providers`, `get_sessionmaker`.
- Produces:
  - **`class MatchState(TypedDict)`** with `user_id`, `docs: LoadedDocuments`, `requirements: list[RequirementDraft]`, `skills: list[SkillDraft]`, `matches: list[SkillMatch]`, `downgraded: int`, `score: ScoreResult | None`.
  - **`build_match_graph(analyst: Analyst) -> CompiledStateGraph`:**
    - Nodes: `extract_jd_requirements`, then `extract_resume_skills`, then `match_with_evidence`, then `validate`, then `score`, then `finalize`.
    - Each node writes `{"type":"progress","step":…}`, with step names from `AnalysisProgress`.
    - Zero requirements after extraction raises `no_requirements` before any further model call.
    - Before calling `analyst.match`, `RequirementDraft`s are converted to `Requirement` (label to `jd_chunk_id`).
  - **`async def analysis_events(*, user_id, sessionmaker, analyst) -> AsyncIterator[tuple[str, BaseModel]]`:**
    1. Emit progress `loading`, then run `load_documents`.
    2. If a cached analysis exists for `(docs, analyst.name)`, yield `done` with `MatchAnalysis(**result, analysis_id=id, cached=True, request_id=…)` and stop; no analyst calls.
    3. Otherwise run the graph, mapping custom events to `progress`.
    4. `save_analysis` runs only after the graph completes successfully.
    5. Yield `done` with `cached=False`.
    - Logs: score, counts, downgraded, cached, analyst name and timings; no text.
  - **`POST /api/stream/analyses`** (no body) returns `sse_response(analysis_events(...))`.

- [ ] **Step 1: Failing unit tests** in `test_match_graph.py`. Use a hand-written `FakeAnalyst` with scripted drafts and call counters.
  - `test_events_and_score`: progress steps arrive in the order `loading`, `extracting_requirements`, `extracting_skills`, `matching`, `validating`, `scoring`, then `done`, with the score per the formula.
  - `test_analysis_downgrades_fabricated_evidence`: one match quotes text that isn't in the resume, giving `downgraded_count == 1`. That skill is `missing`, and the score is lower than with the quote valid.
  - `test_no_requirements_stops_before_match`: extraction returns `[]`, giving an `error` event with code `no_requirements`, and `match` is never called.
- [ ] **Step 2: Failing integration tests** in `test_analyses_api.py`. Use `HeuristicAnalyst` by default, with the provider override pattern from Phase 2 tests.
  - `test_analysis_end_to_end_samples`: `POST /api/samples`, then `POST /api/stream/analyses`. `done.score` is between 0 and 100. A Kafka skill is `matched` with evidence whose `chunk_id` is one of the user's resume chunks. Kubernetes is `missing`. `cached` is false.
  - `test_repeat_analysis_is_cached`: a second POST gives `cached: true` with the same `analysis_id`, and a counting analyst records 0 calls.
  - `test_analysis_requires_both_documents`: with only the JD, the result is an `error` event `documents_required`.
  - `test_analysis_no_requirements`: a JD of plain prose with no Requirements section and no lines the heuristic can use. Use a `FakeAnalyst` override returning `[]`, which gives `no_requirements`.
  - `test_analysis_model_failure_emits_error_and_stores_nothing`: the analyst raises `StructuredOutputError`, giving an `error` event `model_output_invalid` with a `request_id`, and 0 rows in `analyses`.
- [ ] **Step 3: Run.** Expected: FAIL. **Step 4: Implement.** **Step 5: Run** `make lint typecheck test test-integration`. **Step 6: Commit** with message `feat: LangGraph match analysis with grounded evidence and deterministic score`.

---

### Task 6: Suggestion graph and `POST /api/suggestions`

**Files:**
- Create: `app/graphs/suggest.py`, `app/api/suggestions.py`, `tests/unit/test_suggest_graph.py`, `tests/integration/test_suggestions_api.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `Analyst.rewrite`, `validate_suggestions` (T3), repo `load_documents`/`get_analysis`/`latest_analysis` (T4), `MatchAnalysis`, `label_context`, `request_id_var`.
- Produces:
  - **`select_relevant_bullets(resume: Sequence[RetrievedChunk], targets: Sequence[Requirement], *, limit: int) -> list[RetrievedChunk]`:**
    - Candidates are resume chunks in sections `Experience` and `Projects`.
    - Each is scored by the count of distinct target-skill tokens (lowercase, length ≥ 3, not stopwords) appearing in the chunk text.
    - Chunks scoring 0 are dropped; ties break by ordinal; at most `limit` are kept.
  - **`build_suggest_graph(analyst: Analyst, *, limit: int)`:**
    - Nodes: `select_relevant_bullets`, then `rewrite`, then `validate`, then `finalize`.
    - Targets are the analysis's requirements with status `matched` or `partial`, required ones first. When those are empty, use all requirements.
    - When no bullets are selected, `rewrite` is skipped and the suggestion list is empty.
  - **`async def suggest(*, user_id, request: SuggestionRequest, sessionmaker, analyst, limit) -> BulletSuggestions`:**
    - Load the current docs.
    - Resolve the analysis: by `analysis_id` (it must belong to the user and match the current doc ids, else `AppError("analysis_not_found","Analysis not found.",404)`), else `latest_analysis`. If none exists, raise `AppError("analysis_required","Run a match analysis first.",409)`.
    - Run the graph and return `BulletSuggestions`.
  - **`POST /api/suggestions`** (body `SuggestionRequest`, which may be empty `{}`) returns `BulletSuggestions` JSON. Errors use the Phase 2 envelope.

- [ ] **Step 1: Failing unit tests:**
  - `test_select_relevant_bullets_ranks_and_limits`: three Experience chunks with 2, 0 and 1 target tokens give two chunks, in order 2 then 1. Skills/Education chunks are never selected.
  - `test_suggestions_reject_fabricated_rewrite`: the FakeAnalyst returns one clean draft and one "at Google … 40% … Kubernetes" draft. The result is 1 suggestion with `rejected_count == 1`.
  - `test_no_bullets_skips_rewrite`: the rewrite counter is 0 and the result is `[]`.
- [ ] **Step 2: Failing integration tests:**
  - `test_suggestions_end_to_end_samples`: samples, then analysis, then `POST /api/suggestions {}` gives 200. Every `original_chunk_id` is one of the user's resume chunks. `rejected_count == 0` for the heuristic.
  - `test_suggestions_require_analysis`: docs exist but there is no analysis, giving 409 `analysis_required`.
  - `test_suggestions_foreign_analysis_id`: another user's `analysis_id` gives 404 `analysis_not_found`.
  - `test_suggestions_stale_analysis_after_replacement`: analyse, replace the resume, then request suggestions. The old analysis was deleted by the cascade, giving 409 `analysis_required`.
- [ ] **Step 3: Run.** Expected: FAIL. **Step 4: Implement.** **Step 5: Run** `make lint typecheck test test-integration`. **Step 6: Commit** with message `feat: grounded resume bullet suggestions with fabrication guard`.

---

### Task 7: Docs and live smoke

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README section "Phase 3: match analysis and suggestions".** It covers:
  - curl for `POST /api/stream/analyses` (`curl -N`) and `POST /api/suggestions`;
  - the score formula;
  - the grounding and fabrication guards: what gets dropped and downgraded, `downgraded_count` and `rejected_count`;
  - caching and invalidation;
  - offline heuristic versus real Claude (the host-run recipe from Phase 2);
  - the retry and escalation policy (Haiku attempts 2, Sonnet attempts 2, `model_output_invalid`).
- [ ] **Step 2: Run** `make lint typecheck test test-integration` and `docker compose up -d --build --wait`, then run samples, analysis and suggestions against the container. Expected: all succeed offline.
- [ ] **Step 3: Commit** with message `docs: phase 3 match analysis and suggestions`.
- [ ] **Step 4: Live smoke (controller only, needs the human's Anthropic key in `jobmatch/dev`).**
  1. Host-run with `LLM_PROVIDER=anthropic`.
  2. Run samples, then analysis, then suggestions.
  3. Record the score, `downgraded_count`, `rejected_count` and the number of escalations from the logs.
  4. If the key isn't available yet, report it as pending.
