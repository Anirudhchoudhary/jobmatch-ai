from evals.dataset import Expected
from evals.report import render_json, render_markdown
from evals.results import AnalysisResult, CaseResult, ChatResult, DocResult


def _chat(**kw) -> ChatResult:
    base = dict(
        qa_id="q1",
        question="What is jane.doe@example.com's SECRET-QUESTION?",
        kind="answerable",
        expect_refusal=False,
        answer="SECRET-ANSWER jane.doe@example.com",
        refused=False,
        blocked_by=None,
        grounded=True,
        fired=[],
        retrieved=["SECRET-CHUNK"],
        supporting=[],
        expected_answer="SECRET-EXPECTED",
    )
    return ChatResult(**{**base, **kw})


def _result(case_id="poor_match-01", *, score=90, chats=None, errors=None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        category="poor_match",
        variant="system",
        docs=[DocResult(doc_type="resume", attack=False, quarantined=0, fired=[])],
        analysis=AnalysisResult(
            score=score, skills=[("jane.doe@example.com", "matched")], suggestions=1, rejected=0
        ),
        expected=Expected(matched=[], missing=[], partial=[], score_band="poor"),
        chats=chats if chats is not None else [_chat(refused=True)],
        pii_leaks=0,
        cost_usd=0.0,
        latency_ms=5,
        errors=errors or [],
    )


def _render(results, summary=None, failures=None, baseline=None) -> str:
    return render_markdown(
        summary if summary is not None else {"skill_f1": 0.8},
        results,
        profile="offline",
        variant="system",
        subset="ci",
        failures=failures or [],
        baseline=baseline,
    )


def test_markdown_has_no_text_or_pii() -> None:
    md = _render([_result(errors=["internal:ValueError"])])
    for banned in ("jane.doe@example.com", "SECRET-ANSWER", "SECRET-QUESTION", "SECRET-CHUNK"):
        assert banned not in md
    assert "poor_match-01" in md
    assert "q1" in md  # wrong refusal for qa id
    assert "band mismatch" in md
    assert "internal:ValueError" in md


def test_markdown_baseline_delta() -> None:
    md = _render([], {"skill_f1": 0.8}, baseline={"skill_f1": 0.5})
    row = next(line for line in md.splitlines() if "skill_f1" in line or "Skill F1" in line)
    assert "0.80" in row and "0.50" in row and "+0.30" in row


def test_gate_failure_section() -> None:
    md = _render([], failures=["skill_f1 0.41 < min 0.50"])
    gate = md.split("## Gate")[1].split("##")[0]
    assert "skill_f1 0.41 < min 0.50" in gate
    assert "✅" not in gate
    assert "✅" in _render([]).split("## Gate")[1]


def test_guardrails_and_cost_sections() -> None:
    summary = {
        "catch:any:injection": 1.0,
        "catch:provider=bedrock:injection": 0.5,
        "fp:any": 0.0,
        "cost_usd": 0.2,
        "judge_cost_usd": 0.1,
        "latency_p50_ms": 10,
        "latency_p95_ms": 20,
    }
    md = _render([_result()], summary)
    assert "provider=bedrock" in md
    assert "0.30" in md  # total
    assert "n/a" in md


def test_render_json_shape() -> None:
    out = render_json({"a": 1.0}, [_result()], {"profile": "offline"})
    assert out["meta"] == {"profile": "offline"}
    assert out["summary"] == {"a": 1.0}
    assert out["cases"][0]["case_id"] == "poor_match-01"


def test_empty_results() -> None:
    assert "Failing cases" in _render([])
