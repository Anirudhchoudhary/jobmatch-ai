"""The ``Analyst`` interface and its Claude implementation (structured model calls).

Chunks are referenced only by their labels. Validators here check labels and indices so the
runner can feed errors back; quote grounding is checked deterministically afterwards
(``app.analysis.grounding``), not retried. Validator messages never contain document text.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.graphs.citations import format_context
from app.llm.prompts import Prompt, load_prompt
from app.llm.structured import pick_start_index, run_structured
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    EvidenceDraft,
    MatchDraft,
    MatchesOut,
    Requirement,
    RequirementDraft,
    RequirementsOut,
    SkillDraft,
    SkillsOut,
    SuggestionDraft,
    SuggestionsOut,
)

PROMPT_NAMES = ("extract_requirements", "extract_skills", "match_skills", "rewrite_bullets")


class Analyst(Protocol):
    name: str  # cache key: implementation, models and prompt versions

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]: ...

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]: ...

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]: ...

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]: ...


def _model_id(model: BaseChatModel) -> str:
    return str(getattr(model, "model", None) or model._llm_type)


def _labels(labelled: Mapping[str, RetrievedChunk], doc_type: str) -> set[str]:
    return {label for label, c in labelled.items() if c.doc_type == doc_type}


def _evidence_errors(path: str, evidence: Sequence[EvidenceDraft], allowed: set[str]) -> list[str]:
    return [
        f"{path}.evidence.{j}.source: '{ev.source}' is not a supplied resume label"
        for j, ev in enumerate(evidence)
        if ev.source not in allowed
    ]


def _json(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, indent=1)


class ClaudeAnalyst:
    def __init__(
        self,
        models: tuple[BaseChatModel, BaseChatModel],
        *,
        max_attempts: int,
        strong_input_chars: int,
    ) -> None:
        self._models = models
        self._max_attempts = max_attempts
        self._strong_input_chars = strong_input_chars
        self._prompts: dict[str, Prompt] = {n: load_prompt(n) for n in PROMPT_NAMES}
        versions = ":".join(p.version for p in self._prompts.values())
        sha8 = hashlib.sha256(versions.encode()).hexdigest()[:8]
        self.name = f"claude:{_model_id(models[0])}:{_model_id(models[1])}:{sha8}"

    async def _run[T: BaseModel](
        self,
        schema: type[T],
        prompt: str,
        labelled: Mapping[str, RetrievedChunk],
        task: str,
        validate: Callable[[T], list[str]],
    ) -> T:
        messages: list[BaseMessage] = [
            SystemMessage(self._prompts[prompt].text),
            HumanMessage(f"{format_context(labelled)}\n\n{task}"),
        ]
        result = await run_structured(
            schema,
            messages,
            models=self._models,
            step=prompt,
            validate=validate,
            max_attempts_per_model=self._max_attempts,
            start_index=pick_start_index(messages, self._strong_input_chars),
        )
        return result.value

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]:
        allowed = _labels(jd, "jd")

        def validate(out: RequirementsOut) -> list[str]:
            return [
                f"requirements.{i}.source: '{r.source}' is not a supplied job description label"
                for i, r in enumerate(out.requirements)
                if r.source not in allowed
            ]

        task = "Extract the job requirements from the job description chunks above."
        out = await self._run(RequirementsOut, "extract_requirements", jd, task, validate)
        return out.requirements

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]:
        allowed = _labels(resume, "resume")

        def validate(out: SkillsOut) -> list[str]:
            return [
                e
                for i, s in enumerate(out.skills)
                for e in _evidence_errors(f"skills.{i}", s.evidence, allowed)
            ]

        task = "List the skills the resume chunks above demonstrate, with verbatim evidence."
        out = await self._run(SkillsOut, "extract_skills", resume, task, validate)
        return out.skills

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        allowed = _labels(resume, "resume")
        n = len(requirements)

        def validate(out: MatchesOut) -> list[str]:
            errors: list[str] = []
            seen: set[int] = set()
            for i, m in enumerate(out.matches):
                idx = m.requirement_index
                if idx >= n:
                    errors.append(
                        f"matches.{i}.requirement_index: {idx} is out of range (0..{n - 1})"
                    )
                elif idx in seen:
                    errors.append(f"matches.{i}.requirement_index: duplicate index {idx}")
                seen.add(idx)
                errors.extend(_evidence_errors(f"matches.{i}", m.evidence, allowed))
            missing = [k for k in range(n) if k not in seen]
            if missing:
                errors.append(
                    "matches: no match for requirement index " + ", ".join(str(k) for k in missing)
                )
            return errors

        reqs = _json(
            [
                {"index": i, "skill": r.skill, "text": r.text, "importance": r.importance}
                for i, r in enumerate(requirements)
            ]
        )
        extracted = _json(
            [{"skill": s.skill, "evidence": [e.model_dump() for e in s.evidence]} for s in skills]
        )
        task = (
            "Requirements (return exactly one match per index):\n"
            f"{reqs}\n\nSkills extracted from the resume (context):\n{extracted}"
        )
        out = await self._run(MatchesOut, "match_skills", resume, task, validate)
        return out.matches

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]:
        allowed = _labels(bullets, "resume")

        def validate(out: SuggestionsOut) -> list[str]:
            return [
                f"suggestions.{i}.source: '{s.source}' is not a supplied resume label"
                for i, s in enumerate(out.suggestions)
                if s.source not in allowed
            ]

        target_json = _json(
            [{"skill": t.skill, "text": t.text, "importance": t.importance} for t in targets]
        )
        task = (
            "Rewrite resume bullets from the chunks above to address these target "
            f"requirements:\n{target_json}"
        )
        out = await self._run(SuggestionsOut, "rewrite_bullets", bullets, task, validate)
        return out.suggestions
