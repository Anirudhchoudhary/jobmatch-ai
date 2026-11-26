"""Resume bullet suggestions: select relevant bullets -> rewrite -> validate -> finalize.

The model rewrites selected bullets toward the JD's requirements; code drops any rewrite that
introduces a number, term or quote not present in the source bullet (``validate_suggestions``).
No DB session is held while the analyst runs.
"""

import time
from collections.abc import Sequence
from typing import Any, TypedDict
from uuid import UUID

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.analyst import Analyst
from app.analysis.grounding import validate_suggestions
from app.analysis.heuristic import _WORD, _tokens
from app.analysis.repo import get_analysis, latest_analysis, load_documents
from app.core.errors import AppError
from app.core.request_id import request_id_var
from app.graphs.citations import label_context
from app.guardrails.pipeline import GuardrailRecorder, NullRecorder, record_hallucination_block
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    BulletSuggestion,
    BulletSuggestions,
    Requirement,
    SkillMatch,
    SuggestionDraft,
    SuggestionRequest,
)

log = structlog.get_logger(__name__)
_NULL_RECORDER = NullRecorder()

type SuggestGraph = CompiledStateGraph[SuggestState, None, SuggestState, SuggestState]

_BULLET_SECTIONS = frozenset({"Experience", "Projects"})


class SuggestState(TypedDict):
    resume: list[RetrievedChunk]
    skills: list[SkillMatch]
    targets: list[Requirement]
    selected: list[RetrievedChunk]
    # Labels r1..rN over the selection only; shared by the analyst and the validator.
    labelled: dict[str, RetrievedChunk]
    drafts: list[SuggestionDraft]
    suggestions: list[BulletSuggestion]
    rejected: int


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def select_relevant_bullets(
    resume: Sequence[RetrievedChunk], targets: Sequence[Requirement], *, limit: int
) -> list[RetrievedChunk]:
    """Experience/Projects chunks ranked by distinct target-skill tokens they contain."""
    wanted = {t for r in targets for t in _tokens(r.skill)}
    scored: list[tuple[int, int, RetrievedChunk]] = []
    for chunk in resume:
        if chunk.section not in _BULLET_SECTIONS:
            continue
        score = len(wanted & set(_WORD.findall(chunk.text.lower())))
        if score > 0:
            scored.append((-score, chunk.ordinal, chunk))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in scored[:limit]]


def pick_targets(skills: Sequence[SkillMatch]) -> list[Requirement]:
    """Matched/partial requirements, required first; all requirements when none qualify."""
    chosen = [m.requirement for m in skills if m.status in ("matched", "partial")]
    if not chosen:
        chosen = [m.requirement for m in skills]
    return sorted(chosen, key=lambda r: r.importance != "required")


def initial_state(resume: list[RetrievedChunk], skills: list[SkillMatch]) -> SuggestState:
    return {
        "resume": resume,
        "skills": skills,
        "targets": [],
        "selected": [],
        "labelled": {},
        "drafts": [],
        "suggestions": [],
        "rejected": 0,
    }


def build_suggest_graph(analyst: Analyst, *, limit: int) -> SuggestGraph:
    async def select(state: SuggestState) -> dict[str, Any]:
        targets = pick_targets(state["skills"])
        selected = select_relevant_bullets(state["resume"], targets, limit=limit)
        log.info("suggest_selected", selected_count=len(selected), target_count=len(targets))
        return {"targets": targets, "selected": selected, "labelled": label_context(selected)}

    async def rewrite(state: SuggestState) -> dict[str, Any]:
        if not state["selected"]:
            return {"drafts": []}
        start = time.perf_counter()
        drafts = await analyst.rewrite(state["labelled"], state["targets"])
        log.info("suggest_drafted", drafted_count=len(drafts), ms=_ms(start))
        return {"drafts": drafts}

    async def validate(state: SuggestState) -> dict[str, Any]:
        # Guard against every requirement (missing ones included), not just the targets.
        every = [m.requirement for m in state["skills"]]
        kept, rejected = validate_suggestions(
            state["drafts"], state["labelled"], state["targets"], guard=every
        )
        return {"suggestions": kept, "rejected": rejected}

    async def finalize(state: SuggestState) -> dict[str, Any]:
        log.info(
            "suggest_finalized",
            analyst=analyst.name,
            selected_count=len(state["selected"]),
            drafted_count=len(state["drafts"]),
            kept_count=len(state["suggestions"]),
            rejected_count=state["rejected"],
        )
        return {}

    graph = StateGraph(SuggestState)
    graph.add_node("select_relevant_bullets", select)
    graph.add_node("rewrite", rewrite)
    graph.add_node("validate", validate)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "select_relevant_bullets")
    graph.add_edge("select_relevant_bullets", "rewrite")
    graph.add_edge("rewrite", "validate")
    graph.add_edge("validate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def _analysis_not_found() -> AppError:
    return AppError("analysis_not_found", "Analysis not found.", 404)


async def suggest(
    *,
    user_id: UUID,
    request: SuggestionRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    analyst: Analyst,
    limit: int,
    recorder: GuardrailRecorder = _NULL_RECORDER,
) -> BulletSuggestions:
    start = time.perf_counter()
    async with sessionmaker() as session:
        docs = await load_documents(session, user_id)
        analysis_id: UUID
        stored: dict[str, Any]
        if request.analysis_id is not None:
            found = await get_analysis(session, user_id, request.analysis_id)
            if found is None or found[:2] != (docs.resume_doc_id, docs.jd_doc_id):
                raise _analysis_not_found()
            analysis_id, stored = request.analysis_id, found[2]
        else:
            latest = await latest_analysis(session, user_id, docs.resume_doc_id, docs.jd_doc_id)
            if latest is None:
                raise AppError("analysis_required", "Run a match analysis first.", 409)
            analysis_id, stored = latest

    skills = [SkillMatch.model_validate(s) for s in stored["skills"]]
    final = await build_suggest_graph(analyst, limit=limit).ainvoke(
        initial_state(docs.resume, skills)
    )
    if final["rejected"] > 0:
        await record_hallucination_block(
            recorder, user_id, f"suggestions_rejected:{final['rejected']}"
        )
    log.info("suggest_completed", ms=_ms(start))
    return BulletSuggestions(
        analysis_id=analysis_id,
        suggestions=final["suggestions"],
        rejected_count=final["rejected"],
        analyst=analyst.name,
        request_id=request_id_var.get(),
    )
