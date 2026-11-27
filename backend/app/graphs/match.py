"""Match analysis: extract requirements -> extract skills -> match -> validate -> score.

The model proposes requirements, skills and matches by chunk label; code converts labels to
chunk ids, drops ungrounded evidence (downgrading the match) and computes the score. Nodes
report progress through ``get_stream_writer()``; ``analysis_events`` adds loading, the
per-(documents, analyst) cache and persistence around the graph.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, TypedDict, cast
from uuid import UUID

import structlog
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.analyst import Analyst
from app.analysis.grounding import validate_matches
from app.analysis.repo import LoadedDocuments, get_cached_analysis, load_documents, save_analysis
from app.analysis.score import ScoreResult, compute_score
from app.core.errors import AppError
from app.core.request_id import request_id_var
from app.graphs.citations import label_context
from app.guardrails.pipeline import GuardrailRecorder, NullRecorder, record_hallucination_block
from app.llm.structured import StructuredOutputError
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    AnalysisProgress,
    MatchAnalysis,
    MatchDraft,
    Requirement,
    RequirementDraft,
    SkillDraft,
    SkillMatch,
)

log = structlog.get_logger(__name__)
_NULL_RECORDER = NullRecorder()

type MatchGraph = CompiledStateGraph[MatchState, None, MatchState, MatchState]


class MatchState(TypedDict):
    user_id: UUID
    docs: LoadedDocuments
    # Built once from docs.resume + docs.jd: the labels the analyst and the validator share.
    labelled: dict[str, RetrievedChunk]
    requirements: list[RequirementDraft]
    skills: list[SkillDraft]
    drafts: list[MatchDraft]
    matches: list[SkillMatch]
    downgraded: int
    score: ScoreResult | None


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _of_type(labelled: Mapping[str, RetrievedChunk], doc_type: str) -> dict[str, RetrievedChunk]:
    return {label: c for label, c in labelled.items() if c.doc_type == doc_type}


def _progress(step: str) -> None:
    get_stream_writer()({"type": "progress", "step": step})


def _no_requirements() -> AppError:
    return AppError(
        "no_requirements", "We couldn't find any requirements in the job description.", 422
    )


def to_requirements(
    drafts: Sequence[RequirementDraft], labelled: Mapping[str, RetrievedChunk]
) -> list[Requirement]:
    """Requirement drafts must cite a JD label; the label becomes ``jd_chunk_id``."""
    out: list[Requirement] = []
    for d in drafts:
        chunk = labelled.get(d.source)
        if chunk is not None and chunk.doc_type == "jd":
            out.append(
                Requirement(
                    skill=d.skill, text=d.text, importance=d.importance, jd_chunk_id=chunk.chunk_id
                )
            )
    return out


def initial_state(user_id: UUID, docs: LoadedDocuments) -> MatchState:
    return {
        "user_id": user_id,
        "docs": docs,
        "labelled": label_context([*docs.resume, *docs.jd]),
        "requirements": [],
        "skills": [],
        "drafts": [],
        "matches": [],
        "downgraded": 0,
        "score": None,
    }


def build_match_graph(analyst: Analyst) -> MatchGraph:
    async def extract_jd_requirements(state: MatchState) -> dict[str, Any]:
        _progress("extracting_requirements")
        start = time.perf_counter()
        labelled = state["labelled"]
        drafts = await analyst.extract_requirements(_of_type(labelled, "jd"))
        # Keep only drafts citing a JD chunk, so indices stay aligned with to_requirements.
        kept = [d for d in drafts if d.source in labelled and labelled[d.source].doc_type == "jd"]
        log.info(
            "analysis_requirements_extracted",
            requirement_count=len(kept),
            dropped_count=len(drafts) - len(kept),
            ms=_ms(start),
        )
        if not kept:
            raise _no_requirements()
        return {"requirements": kept}

    async def extract_resume_skills(state: MatchState) -> dict[str, Any]:
        _progress("extracting_skills")
        start = time.perf_counter()
        try:
            skills = await analyst.extract_skills(_of_type(state["labelled"], "resume"))
        except StructuredOutputError:
            # Skills are only context for matching: an unusable answer is not fatal (a
            # provider error still is).
            log.warning("skills_extraction_failed", ms=_ms(start))
            return {"skills": []}
        log.info("analysis_skills_extracted", skill_count=len(skills), ms=_ms(start))
        return {"skills": skills}

    async def match_with_evidence(state: MatchState) -> dict[str, Any]:
        _progress("matching")
        start = time.perf_counter()
        drafts = await analyst.match(
            state["requirements"], state["skills"], _of_type(state["labelled"], "resume")
        )
        log.info("analysis_matched", match_count=len(drafts), ms=_ms(start))
        return {"drafts": drafts}

    async def validate(state: MatchState) -> dict[str, Any]:
        _progress("validating")
        requirements = to_requirements(state["requirements"], state["labelled"])
        # The combined mapping lets the validator reject evidence that cites a JD chunk.
        matches, downgraded = validate_matches(state["drafts"], requirements, state["labelled"])
        log.info("analysis_validated", match_count=len(matches), downgraded_count=downgraded)
        return {"matches": matches, "downgraded": downgraded}

    async def score(state: MatchState) -> dict[str, Any]:
        _progress("scoring")
        return {"score": compute_score(state["matches"])}

    async def finalize(state: MatchState) -> dict[str, Any]:
        result = state["score"]
        log.info(
            "analysis_finalized",
            analyst=analyst.name,
            score=result.score if result else None,
            requirement_count=len(state["matches"]),
            downgraded_count=state["downgraded"],
        )
        return {}

    graph = StateGraph(MatchState)
    graph.add_node("extract_jd_requirements", extract_jd_requirements)
    graph.add_node("extract_resume_skills", extract_resume_skills)
    graph.add_node("match_with_evidence", match_with_evidence)
    graph.add_node("validate", validate)
    graph.add_node("score", score)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "extract_jd_requirements")
    graph.add_edge("extract_jd_requirements", "extract_resume_skills")
    graph.add_edge("extract_resume_skills", "match_with_evidence")
    graph.add_edge("match_with_evidence", "validate")
    graph.add_edge("validate", "score")
    graph.add_edge("score", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def _result(state: MatchState, analyst: str) -> dict[str, Any]:
    """The stored analysis: ``MatchAnalysis`` minus per-request fields, JSON-ready."""
    score = state["score"]
    if score is None:  # pragma: no cover - the graph always scores before finishing
        raise RuntimeError("match graph finished without a score")
    return {
        "score": score.score,
        "required_coverage": score.required_coverage,
        "nice_to_have_coverage": score.nice_to_have_coverage,
        "skills": [m.model_dump(mode="json") for m in state["matches"]],
        "downgraded_count": state["downgraded"],
        "analyst": analyst,
    }


async def analysis_events(
    *,
    user_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    analyst: Analyst,
    before_fresh: Callable[[], Awaitable[None]] | None = None,
    recorder: GuardrailRecorder = _NULL_RECORDER,
) -> AsyncIterator[tuple[str, BaseModel]]:
    """``progress`` events then ``done`` (a ``MatchAnalysis``). No DB session is held while
    the analyst runs; the analysis is saved only after the graph succeeds."""
    start = time.perf_counter()
    yield "progress", AnalysisProgress(step="loading")
    async with sessionmaker() as session:
        docs = await load_documents(session, user_id)
        cached = await get_cached_analysis(
            session, user_id, docs.resume_doc_id, docs.jd_doc_id, analyst.name
        )
    if cached is not None:
        analysis_id, stored = cached
        done = MatchAnalysis(
            **stored, analysis_id=analysis_id, cached=True, request_id=request_id_var.get()
        )
        _log_completed(done, start)
        yield "done", done
        return

    if before_fresh is not None:
        await before_fresh()

    final = initial_state(user_id, docs)
    graph_start = time.perf_counter()
    async for mode, raw in build_match_graph(analyst).astream(
        final, stream_mode=["custom", "values"]
    ):
        if mode == "values":
            final = cast(MatchState, raw)
            continue
        part = cast(dict[str, Any], raw)
        if part["type"] == "progress":
            yield "progress", AnalysisProgress(step=part["step"])
    graph_ms = _ms(graph_start)
    if final["downgraded"] > 0:
        await record_hallucination_block(
            recorder, user_id, f"evidence_downgraded:{final['downgraded']}"
        )

    result = _result(final, analyst.name)
    async with sessionmaker() as session:
        analysis_id = await save_analysis(
            session,
            user_id=user_id,
            resume_doc_id=docs.resume_doc_id,
            jd_doc_id=docs.jd_doc_id,
            analyst=analyst.name,
            result=result,
        )
    done = MatchAnalysis(
        **result, analysis_id=analysis_id, cached=False, request_id=request_id_var.get()
    )
    _log_completed(done, start, graph_ms=graph_ms)
    yield "done", done


def _log_completed(done: MatchAnalysis, start: float, *, graph_ms: int | None = None) -> None:
    statuses = [s.status for s in done.skills]
    log.info(
        "analysis_completed",
        analyst=done.analyst,
        cached=done.cached,
        score=done.score,
        requirement_count=len(statuses),
        matched_count=statuses.count("matched"),
        partial_count=statuses.count("partial"),
        missing_count=statuses.count("missing"),
        downgraded_count=done.downgraded_count,
        graph_ms=graph_ms,
        ms=_ms(start),
    )
