"""Markdown and JSON reports for an eval run.

The markdown is posted as a PR comment, so it carries only ids, categories, metrics, counts
and error codes: never answers, questions, document text or PII.
"""

from collections.abc import Sequence
from typing import Any

from evals.dataset import score_band
from evals.metrics import refused_like
from evals.results import CaseResult

_QUALITY = (
    ("skill_f1", "Skill F1"),
    ("score_band_accuracy", "Score-band accuracy"),
    ("refusal_correctness", "Refusal correctness"),
    ("hallucination_block_rate", "Hallucination-block rate"),
    ("answerable_answer_rate", "Answerable answer rate"),
    ("context_hit_rate", "Context hit rate"),
    ("analysis_error_rate", "Analysis error rate"),
    ("pii_leaks", "PII leaks"),
)
_CLASSES = ("injection", "off_topic", "pii")


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


def _quality(summary: dict[str, float], baseline: dict[str, float] | None) -> list[str]:
    metrics = [(k, label) for k, label in _QUALITY if k in summary]
    metrics += [(k, f"Judge {k[6:]}") for k in sorted(summary) if k.startswith("judge:")]
    if baseline is None:
        return _table(["metric", "value"], [[label, _num(summary[k])] for k, label in metrics])
    rows = []
    for k, label in metrics:
        base = baseline.get(k)
        delta = "n/a" if base is None else f"{summary[k] - base:+.2f}"
        rows.append([label, _num(summary[k]), _num(base), delta])
    return _table(["metric", "value", "baseline", "Δ"], rows)


def _guardrails(summary: dict[str, float]) -> list[str]:
    names = list(
        {k.split(":")[1] for k in summary if k.startswith("catch:")}
        | {k[3:] for k in summary if k.startswith("fp:")}
    )
    # any, then provider groups, then individual guardrails
    names.sort(key=lambda n: (0 if n == "any" else 1 if n.startswith("provider=") else 2, n))
    rows = [
        [n]
        + [_num(summary.get(f"catch:{n}:{c}")) for c in _CLASSES]
        + [_num(summary.get(f"fp:{n}"))]
        for n in names
    ]
    return _table(["guardrail", *[f"catch {c}" for c in _CLASSES], "FP"], rows)


def _failing(results: Sequence[CaseResult]) -> list[str]:
    lines: list[str] = []
    for r in results:
        reasons: list[str] = []
        if r.analysis is None:
            reasons.append("no analysis")
        elif score_band(r.analysis.score) != r.expected.score_band:
            reasons.append(
                f"band mismatch (got {score_band(r.analysis.score)}, "
                f"expected {r.expected.score_band})"
            )
        reasons += [
            f"wrong refusal for {c.qa_id}" for c in r.chats if refused_like(c) != c.expect_refusal
        ]
        reasons += [f"error {code}" for code in r.errors]
        if r.pii_leaks:
            reasons.append(f"{r.pii_leaks} PII leaks")
        if reasons:
            lines.append(f"- `{r.case_id}` ({r.category}): " + "; ".join(reasons))
    return lines or ["None."]


def render_markdown(
    summary: dict[str, float],
    results: Sequence[CaseResult],
    *,
    profile: str,
    variant: str,
    subset: str,
    failures: list[str],
    baseline: dict[str, float] | None,
) -> str:
    system_cost = summary.get("cost_usd", 0.0)
    judge_cost = summary.get("judge_cost_usd")
    total = system_cost + (judge_cost or 0.0)
    n = len(results)
    out = [
        "# Eval report",
        "",
        f"profile `{profile}` | variant `{variant}` | subset `{subset}` | {n} cases | "
        f"cost ${total:.4f}",
        "",
        "## Gate",
        "",
        *(["✅ All thresholds met."] if not failures else [f"- {f}" for f in failures]),
        "",
        "## Quality",
        "",
        *_quality(summary, baseline),
        "",
        "## Guardrails",
        "",
        *_guardrails(summary),
        "",
        "## Latency & cost",
        "",
        f"- p50 latency: {summary.get('latency_p50_ms', 0):.0f} ms",
        f"- p95 latency: {summary.get('latency_p95_ms', 0):.0f} ms",
        f"- System cost: ${system_cost:.4f}",
    ]
    if judge_cost is not None:
        out.append(f"- Judge cost: ${judge_cost:.4f}")
    out += [
        f"- Total cost: ${total:.4f}",
        f"- Cost per case: ${(total / n if n else 0.0):.4f}",
        "",
        "## Failing cases",
        "",
        *_failing(results),
        "",
    ]
    return "\n".join(out)


def render_json(
    summary: dict[str, float], results: Sequence[CaseResult], meta: dict[str, Any]
) -> dict[str, Any]:
    return {
        "meta": meta,
        "summary": summary,
        "cases": [r.model_dump() for r in results],
    }
