"""Runs golden cases through the real pipeline (ingest, analysis, suggestions, chat).

One profile and variant per process: `eval_settings` applies its overrides to `os.environ`
and clears the settings/provider/guardrail caches, so app code that calls `get_settings()`
internally sees the same configuration. Nothing is restored; the CLI exits afterwards.
"""

import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.db.models import Chunk, User
from app.graphs.chat import ChatState, build_chat_graph
from app.graphs.match import analysis_events
from app.graphs.suggest import suggest
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.factory import (
    Guardrails,
    bedrock_runtime_client,
    build_guardrails,
    get_guardrails,
    guard_models,
)
from app.ingestion.blobs import LocalBlobStore, new_upload_key
from app.ingestion.service import ingest_document
from app.llm.prompts import load_prompt
from app.llm.providers import Providers, build_providers, get_providers
from app.observability.usage import usage_scope
from app.retrieval.service import Retriever
from app.schemas.documents import DocumentReady, DocumentRequest
from app.schemas.match import MatchAnalysis, SuggestionRequest
from evals.dataset import DATASET_DIR, EvalCase
from evals.results import AnalysisResult, CaseResult, ChatResult, DocResult, Fired

log = structlog.get_logger(__name__)

_HANDLER_MARK = "_jobmatch_handler"  # set by app.core.logging.configure_logging

_GUARDS_OFF = {
    "GUARD_INJECTION_RULES": "off",
    "GUARD_INJECTION_CLASSIFIER": "off",
    "GUARD_TOPIC": "off",
    "GUARD_PII": "off",
    "GUARD_GROUNDEDNESS": "off",
    "GUARD_BEDROCK": "off",
}
VARIANTS: dict[str, dict[str, str]] = {
    "system": {},
    "baseline": {"RETRIEVAL_MODE": "vector", "RERANKER_PROVIDER": "none", **_GUARDS_OFF},
}
PROFILES: dict[str, dict[str, str]] = {
    "offline": {
        "LLM_PROVIDER": "fake",
        "EMBEDDER_PROVIDER": "hash",
        "RERANKER_PROVIDER": "none",
        "GUARD_BEDROCK": "off",
    },
    "live": {
        "LLM_PROVIDER": "anthropic",
        "EMBEDDER_PROVIDER": "bedrock",
        "RERANKER_PROVIDER": "bedrock",
    },
}


_applied: tuple[str, str] | None = None  # first (profile, variant); see eval_settings


def eval_settings(profile: str, variant: str) -> Settings:
    """Settings for a profile and variant (variant overrides win).

    Also writes the overrides to `os.environ` and clears the cached getters; see the module
    docstring for why nothing is restored. A second call with a different (profile, variant)
    raises RuntimeError; the same pair is fine."""
    global _applied
    if _applied is not None and _applied != (profile, variant):
        raise RuntimeError(
            f"eval_settings already applied {_applied}; one profile/variant per process"
        )
    overrides = {**PROFILES[profile], **VARIANTS[variant]}
    base = get_settings()
    settings = Settings.model_validate(
        {**base.model_dump(), **{k.lower(): v for k, v in overrides.items()}}
    )
    _applied = (profile, variant)
    os.environ.update(overrides)
    for cached in (get_settings, get_providers, get_guardrails):
        cached.cache_clear()
    return settings


class CaptureRecorder:
    """Keeps guardrail outcomes in memory (nothing is written to the DB)."""

    def __init__(self) -> None:
        self.records: list[tuple[GuardContext, GuardrailOutcome]] = []

    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        self.records.append((ctx, outcome))

    def fired_since(self, mark: int, *stages: str) -> list[Fired]:
        """Outcomes recorded after `mark` for the given stages (the pipeline records only
        non-`allow` outcomes and errors)."""
        return [
            Fired(guardrail=o.guardrail, provider=o.provider, action=o.action, mode=o.mode)
            for _, o in self.records[mark:]
            if o.stage in stages
        ]


def count_leaks(lines: Sequence[str], values: Sequence[str]) -> int:
    """Occurrences of any seeded PII value across the rendered log lines."""
    return sum(line.count(value) for line in lines for value in values)


class _CaptureHandler(logging.Handler):
    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _app_formatter(settings: Settings) -> logging.Formatter:
    """The formatter the app's own log handler uses, so captured lines are what would ship."""
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARK, False) and handler.formatter is not None:
            return handler.formatter
    configure_logging(settings.log_level, pii_engine=settings.pii_engine)
    return _app_formatter(settings)


@contextmanager
def capture_logs(settings: Settings) -> Iterator[list[str]]:
    """Collect every rendered (redacted) log line emitted inside the block."""
    handler = _CaptureHandler(_app_formatter(settings))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)


@asynccontextmanager
async def _stage(name: str, errors: list[str]) -> AsyncIterator[None]:
    """Record a stage failure as an error code and let the case continue."""
    try:
        yield
    except AppError as exc:
        errors.append(exc.code)
    except Exception as exc:  # noqa: BLE001
        log.error("eval_stage_failed", stage=name, error_type=type(exc).__name__)
        errors.append(f"internal:{type(exc).__name__}")


async def _quarantined(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID, doc_type: str
) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.user_id == user_id,
                Chunk.doc_type == doc_type,
                Chunk.quarantined.is_(True),
            )
        )
    return count or 0


def _retriever(
    settings: Settings, providers: Providers, sessionmaker: async_sessionmaker[AsyncSession]
) -> Retriever:
    return Retriever(
        sessionmaker,
        providers.embedder,
        providers.reranker,
        mode=settings.retrieval_mode,
        k=settings.retrieval_k,
        rrf_k=settings.rrf_k,
        top_n=settings.rerank_top_n,
    )


async def run_case(
    case: EvalCase,
    *,
    settings: Settings,
    variant: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    blobs_dir: Path,
    pdf_dir: Path = DATASET_DIR / "pdfs",
    providers: Providers | None = None,
) -> CaseResult:
    """Run one case as a fresh user (deleted afterwards). `providers` may be shared across
    cases; guardrails are built per case so each case has its own recorder."""
    providers = providers or build_providers(settings)
    recorder = CaptureRecorder()
    guardrails: Guardrails = build_guardrails(
        settings,
        recorder,
        models=guard_models(settings),
        bedrock_client=bedrock_runtime_client(settings),
    )
    blobs = LocalBlobStore(blobs_dir)
    user_id = uuid4()
    errors: list[str] = []
    docs: list[DocResult] = []
    analysis: AnalysisResult | None = None
    chats: list[ChatResult] = []
    started = time.perf_counter()

    async with sessionmaker() as session:
        session.add(User(id=user_id))
        await session.commit()

    async def ingest(doc_type: str, request: DocumentRequest, attack: bool) -> None:
        mark = len(recorder.records)
        async with _stage(f"ingest_{doc_type}", errors):
            async for _event, payload in ingest_document(
                user_id=user_id,
                request=request,
                sessionmaker=sessionmaker,
                blobs=blobs,
                embedder=providers.embedder,
                settings=settings,
                guardrails=guardrails,
            ):
                if isinstance(payload, DocumentReady):
                    break
        docs.append(
            DocResult(
                doc_type="resume" if doc_type == "resume" else "jd",
                attack=attack,
                quarantined=await _quarantined(sessionmaker, user_id, doc_type),
                fired=recorder.fired_since(mark, "upload"),
            )
        )

    try:
        with capture_logs(settings) as lines, usage_scope() as usage:
            key = new_upload_key(user_id)
            await blobs.put(key, (pdf_dir / f"{case.id}.pdf").read_bytes())
            await ingest(
                "resume",
                DocumentRequest(doc_type="resume", upload_key=key),
                case.attacks.resume_injection,
            )
            await ingest(
                "jd", DocumentRequest(doc_type="jd", text=case.jd), case.attacks.jd_injection
            )

            done: MatchAnalysis | None = None
            async with _stage("analysis", errors):
                async for event, payload in analysis_events(
                    user_id=user_id,
                    sessionmaker=sessionmaker,
                    analyst=providers.analyst,
                    recorder=recorder,
                ):
                    if event == "done" and isinstance(payload, MatchAnalysis):
                        done = payload
            if done is not None:
                suggestions = rejected = 0
                async with _stage("suggest", errors):
                    result = await suggest(
                        user_id=user_id,
                        request=SuggestionRequest(analysis_id=done.analysis_id),
                        sessionmaker=sessionmaker,
                        analyst=providers.analyst,
                        limit=settings.suggest_max_bullets,
                        recorder=recorder,
                    )
                    suggestions, rejected = len(result.suggestions), result.rejected_count
                analysis = AnalysisResult(
                    score=done.score,
                    skills=[(m.requirement.skill, m.status) for m in done.skills],
                    suggestions=suggestions,
                    rejected=rejected,
                )

            prompt = load_prompt("chat_system")
            # No semantic cache: every case is a new user, and hits would hide latency/cost.
            graph = build_chat_graph(
                _retriever(settings, providers, sessionmaker),
                providers.chat_model,
                prompt,
                min_relevance=settings.min_relevance,
                guardrails=guardrails,
                cache=None,
            )
            for qa in case.qa:
                mark = len(recorder.records)
                state: ChatState = {
                    "user_id": user_id,
                    "question": qa.question,
                    "chunks": [],
                    "answer": "",
                    "refused": False,
                    "guard_outcomes": [],
                    "blocked_by": None,
                    "verdict": None,
                    "query_embedding": None,
                    "cache_key": None,
                    "cached": None,
                }
                async with _stage(f"chat_{qa.id}", errors):
                    final = await graph.ainvoke(state)
                    verdict = final["verdict"]
                    chats.append(
                        ChatResult(
                            qa_id=qa.id,
                            question=qa.question,
                            kind=qa.kind,
                            expect_refusal=qa.expect_refusal,
                            answer=final["answer"],
                            refused=final["refused"],
                            blocked_by=final["blocked_by"],
                            grounded=verdict.grounded if verdict is not None else None,
                            fired=recorder.fired_since(mark, "chat_input", "output"),
                            retrieved=[c.text for c in final["chunks"]],
                            supporting=qa.supporting,
                            expected_answer=qa.expected_answer,
                        )
                    )
            cost = usage.cost_usd
        pii_leaks = count_leaks(lines, case.pii_values)
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()

    return CaseResult(
        case_id=case.id,
        category=case.category,
        variant=variant,
        docs=docs,
        analysis=analysis,
        expected=case.expected,
        chats=chats,
        pii_leaks=pii_leaks,
        cost_usd=cost,
        latency_ms=int((time.perf_counter() - started) * 1000),
        errors=errors,
    )


async def run_all(cases: Sequence[EvalCase], *, profile: str, variant: str) -> list[CaseResult]:
    """Run the cases one after another (never concurrently: logs and the ledger are per case)."""
    settings = eval_settings(profile, variant)
    providers = build_providers(settings)
    sessionmaker = get_sessionmaker()
    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="jobmatch-evals-") as blobs_dir:
        for case in cases:
            results.append(
                await run_case(
                    case,
                    settings=settings,
                    variant=variant,
                    sessionmaker=sessionmaker,
                    blobs_dir=Path(blobs_dir),
                    providers=providers,
                )
            )
    return results
