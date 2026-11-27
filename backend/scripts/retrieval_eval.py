"""Retrieval baseline: recall@5 and MRR for vector, hybrid and hybrid+rerank.

Ingests the bundled samples for a throwaway user (deleted afterwards, cascade), runs every
question in evals/retrieval/questions.json per mode, prints a markdown table and writes JSON.
Providers come from Settings, so the live run only flips env vars (see `make retrieval-eval-live`).
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.samples import ingest_samples
from app.core.settings import Settings, get_settings
from app.db.engine import make_engine
from app.db.models import User
from app.ingestion.blobs import LocalBlobStore
from app.llm.providers import Providers, get_providers
from app.retrieval.search import RetrievedChunk
from app.retrieval.service import Retriever

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONS = REPO_ROOT / "evals" / "retrieval" / "questions.json"
DEFAULT_OUT = REPO_ROOT / "var" / "retrieval_baseline.offline.json"
COMMITTED_DIR = REPO_ROOT / "evals"
ALL_MODES = ("vector", "hybrid", "hybrid+rerank")
K = 5


def score(ranked: list[RetrievedChunk], expected: str) -> tuple[bool, float]:
    """(hit in top 5, reciprocal rank of the first hit within the top 5)."""
    needle = expected.lower()
    for rank, chunk in enumerate(ranked[:K], start=1):
        if needle in chunk.text.lower():
            return True, 1 / rank
    return False, 0.0


async def _ranked(
    retriever: Retriever, mode: str, user_id: UUID, question: str
) -> list[RetrievedChunk]:
    candidates = await retriever.candidates(user_id, question)
    return await retriever.rerank(question, candidates) if mode == "hybrid+rerank" else candidates


async def evaluate(
    *,
    modes: list[str],
    questions: list[dict[str, str]],
    sessionmaker: async_sessionmaker[AsyncSession],
    providers: Providers,
    settings: Settings,
    blobs_dir: Path,
) -> dict[str, Any]:
    user_id = uuid4()
    async with sessionmaker() as session:
        session.add(User(id=user_id))
        await session.commit()
    try:
        async for name, _ in ingest_samples(
            user_id=user_id,
            sessionmaker=sessionmaker,
            blobs=LocalBlobStore(blobs_dir),
            embedder=providers.embedder,
            settings=settings,
        ):
            if name == "error":  # pragma: no cover - ingest errors raise before this
                raise RuntimeError("sample ingest failed")
        results: dict[str, Any] = {}
        for mode in modes:
            if mode == "hybrid+rerank" and settings.reranker_provider == "none":
                results[mode] = {"skipped": "reranker=none"}
                continue
            retriever = Retriever(
                sessionmaker,
                providers.embedder,
                providers.reranker,
                mode="vector" if mode == "vector" else "hybrid",
                k=settings.retrieval_k,
                rrf_k=settings.rrf_k,
                top_n=settings.rerank_top_n,
            )
            hits, rr = 0, 0.0
            for q in questions:
                hit, recip = score(
                    await _ranked(retriever, mode, user_id, q["question"]), q["expected_substring"]
                )
                hits += hit
                rr += recip
            n = len(questions)
            results[mode] = {"recall_at_5": hits / n, "mrr": rr / n, "n": n}
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
    return {
        "providers": {
            "embedder": settings.embedder_provider,
            "reranker": settings.reranker_provider,
            "embed_model_id": settings.embed_model_id,
        },
        "modes": results,
    }


def render_table(report: dict[str, Any]) -> str:
    lines = ["| mode | recall@5 | MRR | n |", "|---|---|---|---|"]
    for mode, r in report["modes"].items():
        if "skipped" in r:
            lines.append(f"| {mode} | skipped ({r['skipped']}) | | |")
        else:
            lines.append(f"| {mode} | {r['recall_at_5']:.3f} | {r['mrr']:.3f} | {r['n']} |")
    return "\n".join(lines)


def _guard_committed_output(out: Path, providers: Providers) -> None:
    """Only real (Bedrock) embeddings may write under evals/: that baseline is committed."""
    if out.resolve().is_relative_to(COMMITTED_DIR.resolve()) and not (
        providers.embedder.embed_model.startswith("bedrock:")
    ):
        print(
            f"refusing to write {out}: files under evals/ are committed baselines and need "
            "EMBEDDER_PROVIDER=bedrock (use `make retrieval-eval-live`); "
            "offline runs write to var/ instead.",
            file=sys.stderr,
        )
        raise SystemExit(2)


async def _run(
    args: argparse.Namespace, questions: list[dict[str, str]], providers: Providers
) -> dict[str, Any]:
    settings = get_settings()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = set(modes) - set(ALL_MODES)
    if unknown:
        raise SystemExit(f"unknown modes: {sorted(unknown)}")
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return await evaluate(
                modes=modes,
                questions=questions,
                sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
                providers=providers,
                settings=settings,
                blobs_dir=Path(tmp),
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None, *, providers: Providers | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", default=",".join(ALL_MODES))
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    out = Path(args.out)
    providers = providers or get_providers()
    _guard_committed_output(out, providers)
    questions = cast(list[dict[str, str]], json.loads(Path(args.questions).read_text()))
    report = asyncio.run(_run(args, questions, providers))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(render_table(report))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
