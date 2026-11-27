"""Run the golden eval suite and write `report.md` / `report.json`.

    python -m scripts.run_evals --profile offline --variant system --subset full --out DIR

Offline needs only the local Postgres. Live (`--profile live`) calls Anthropic and Bedrock and
spends money; it needs ANTHROPIC_API_KEY and imports the DeepEval judge lazily.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.observability.usage import usage_scope
from evals.dataset import load_cases
from evals.metrics import summarize
from evals.report import render_json, render_markdown
from evals.results import CaseResult
from evals.runner import eval_settings, run_all
from evals.thresholds import check, load_thresholds

THRESHOLDS = Path(__file__).resolve().parents[2] / "evals" / "thresholds.yaml"


async def _run(profile: str, variant: str, subset: str) -> tuple[list[CaseResult], float | None]:
    cases = load_cases(subset=subset)  # type: ignore[arg-type]
    results = await run_all(cases, profile=profile, variant=variant)
    if profile != "live":
        return results, None
    from evals.judge import judge_chats, make_judge  # lazy: offline needs no deepeval

    judge = make_judge(eval_settings(profile, variant))
    with usage_scope() as usage:
        await judge_chats([c for r in results for c in r.chats], judge)
    return results, usage.cost_usd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["offline", "live"], required=True)
    parser.add_argument("--variant", choices=["system", "baseline"], default="system")
    parser.add_argument("--subset", choices=["ci", "full"], default="full")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="previous report.json (or baseline.json)")
    parser.add_argument("--no-gate", action="store_true")
    parser.add_argument("--write-baseline", type=Path)
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    args = parser.parse_args(argv)

    try:
        results, judge_cost = asyncio.run(_run(args.profile, args.variant, args.subset))
    except ValidationError as exc:
        reasons = "; ".join(e["msg"] for e in exc.errors())
        print(f"invalid settings for profile {args.profile!r}: {reasons}", file=sys.stderr)
        return 2

    summary = summarize(results)
    if judge_cost is not None:
        summary["judge_cost_usd"] = judge_cost
    failures = check(summary, load_thresholds(args.thresholds, args.profile))
    baseline = json.loads(args.baseline.read_text())["summary"] if args.baseline else None
    markdown = render_markdown(
        summary,
        results,
        profile=args.profile,
        variant=args.variant,
        subset=args.subset,
        failures=failures,
        baseline=baseline,
    )
    meta: dict[str, Any] = {
        "profile": args.profile,
        "variant": args.variant,
        "subset": args.subset,
        "cases": len(results),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(markdown, encoding="utf-8")
    (args.out / "report.json").write_text(
        json.dumps(render_json(summary, results, meta), indent=2) + "\n", encoding="utf-8"
    )
    if args.write_baseline:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(
            json.dumps({"meta": meta, "summary": summary}, indent=2) + "\n", encoding="utf-8"
        )
    print(markdown)
    return 1 if failures and not args.no_gate else 0


if __name__ == "__main__":
    sys.exit(main())
