import pytest

from evals.dataset import Expected
from evals.metrics import normalize_skill, skill_f1, summarize
from evals.results import AnalysisResult, CaseResult, ChatResult, DocResult, Fired


def _chat(kind="answerable", **kw) -> ChatResult:
    base = {
        "qa_id": "q1",
        "question": "?",
        "kind": kind,
        "expect_refusal": kind != "answerable",
        "answer": "",
        "refused": False,
        "blocked_by": None,
        "grounded": None,
        "fired": [],
        "retrieved": [],
        "supporting": [],
        "expected_answer": None,
    }
    return ChatResult(**{**base, **kw})


def _case(chats=(), docs=(), analysis=None, band="strong", errors=(), **kw) -> CaseResult:
    return CaseResult(
        case_id="c-01",
        category="strong_match",
        variant="full",
        docs=list(docs),
        analysis=analysis,
        expected=Expected(matched=["Python"], missing=[], score_band=band),
        chats=list(chats),
        pii_leaks=kw.get("pii_leaks", 0),
        cost_usd=kw.get("cost_usd", 0.0),
        latency_ms=kw.get("latency_ms", 0),
        errors=list(errors),
    )


def _fired(guardrail, provider="self", action="block", mode="enforce", stage="chat_input") -> Fired:
    return Fired(guardrail=guardrail, provider=provider, action=action, mode=mode, stage=stage)


def test_normalize_skill_aliases():
    assert normalize_skill("K8s") == "kubernetes"
    assert normalize_skill(" Node.js ") == "nodejs"


def test_skill_f1_aliases_and_partial():
    exp = Expected(
        matched=["Kubernetes"], partial=["Python"], missing=["Rust"], score_band="partial"
    )
    full = [("k8s", "matched"), ("Python", "partial"), ("Rust", "missing")]
    assert skill_f1(exp, full) == 1.0
    assert skill_f1(exp, [("k8s", "matched"), ("Rust", "missing")]) < 1.0


def test_skill_f1_whole_word_substring_and_empty():
    exp = Expected(matched=["REST APIs"], missing=[], score_band="strong")
    assert skill_f1(exp, [("REST", "matched")]) == 1.0
    assert (
        skill_f1(Expected(matched=["C"], missing=[], score_band="poor"), [("C++", "matched")]) < 1
    )
    assert skill_f1(Expected(matched=[], missing=[], score_band="poor"), []) == 1.0


def test_monitor_firing_is_detection_not_refusal():
    chat = _chat(
        "injection",
        fired=[_fired("bedrock_guardrails", "bedrock", mode="monitor")],
    )
    s = summarize([_case([chat])])
    assert s["catch:bedrock_guardrails:injection"] == 1.0
    assert s["catch:provider=bedrock:injection"] == 1.0
    assert s["catch:provider=self_built:injection"] == 0.0
    assert s["catch:any:injection"] == 1.0
    assert s["refusal_correctness"] == 0.0


def test_fp_rate_counts_benign_items():
    chats = [_chat(fired=[_fired("injection_rules")]), _chat()]
    s = summarize([_case(chats)])
    assert s["fp:injection_rules"] == 0.5
    assert s["fp:provider=self_built"] == 0.5
    assert s["fp:any"] == 0.5
    assert s["fp:provider=bedrock"] == 0.0


def test_doc_attacks_feed_injection_class():
    doc = DocResult(
        doc_type="resume", attack=True, quarantined=1, fired=[_fired("injection_rules")]
    )
    s = summarize([_case(docs=[doc])])
    assert s["catch:injection_rules:injection"] == 1.0
    assert "fp:any" not in s  # no benign items


def test_empty_classes_omit_catch_keys():
    s = summarize([_case([_chat()])])
    assert not any(k.startswith("catch:") for k in s)
    assert s["fp:any"] == 0.0


def test_score_band_and_error_rate():
    ok = _case(analysis=AnalysisResult(score=72, skills=[], suggestions=0, rejected=0))
    bad = _case(errors=["no_requirements"])
    s = summarize([ok, bad])
    assert s["score_band_accuracy"] == 0.5
    assert s["analysis_error_rate"] == 0.5


def test_hallucination_block_rate():
    chats = [_chat("fabrication", grounded=False), _chat("fabrication")]
    assert summarize([_case(chats)])["hallucination_block_rate"] == 0.5


def test_answer_and_context_rates_and_totals():
    hit = _chat(supporting=["Python"], retrieved=["I know python well"])
    miss = _chat(supporting=["Go"], retrieved=["nothing"], refused=True)
    s = summarize(
        [_case([hit, miss], pii_leaks=1, cost_usd=0.25, latency_ms=100), _case(latency_ms=300)]
    )
    assert s["answerable_answer_rate"] == 0.5
    assert s["context_hit_rate"] == 0.5
    assert s["pii_leaks"] == 1
    assert s["cost_usd"] == 0.25
    assert s["latency_p50_ms"] == 100
    assert s["latency_p95_ms"] == 300


def test_judge_means_only_when_present():
    s = summarize([_case([_chat(judge={"faithfulness": 0.5}), _chat(judge={"faithfulness": 1.0})])])
    assert s["judge:faithfulness"] == 0.75
    assert not any(k.startswith("judge:") and k != "judge:faithfulness" for k in s)


@pytest.mark.parametrize("n", [0])
def test_empty_results(n):
    assert summarize([])["pii_leaks"] == 0


def test_output_stage_firings_do_not_feed_catch_or_fp():
    out_only = [_fired("groundedness", stage="output")]
    chats = [
        _chat("off_topic", fired=out_only),
        _chat("answerable", fired=out_only),
        _chat("pii", fired=[_fired("pii")]),
    ]
    s = summarize([_case(chats=chats)])
    assert s["catch:any:off_topic"] == 0.0
    assert s["catch:any:pii"] == 1.0
    assert s["fp:any"] == 0.0
    assert "catch:groundedness:off_topic" not in s


def test_node_js_does_not_match_javascript():
    exp = Expected(matched=["JavaScript"], missing=[], score_band="strong")
    assert skill_f1(exp, [("Node.js", "matched")]) == 0.5  # covered F1 0, empty-missing F1 1
    assert skill_f1(exp, [("JS", "matched")]) == 1.0


def test_aliases_still_match_whole_names():
    assert normalize_skill("Postgres") == normalize_skill("PostgreSQL")
    exp = Expected(matched=["Kubernetes", "PostgreSQL"], missing=[], score_band="strong")
    assert skill_f1(exp, [("k8s", "matched"), ("Postgres", "matched")]) == 1.0
