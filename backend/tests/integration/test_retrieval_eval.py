import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.analysis.heuristic import HeuristicAnalyst
from app.core.settings import get_settings
from app.llm.chat import make_chat_model
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers
from app.llm.rerank import NoopReranker
from scripts import retrieval_eval
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration


def _counts() -> tuple[int, int]:
    eng = create_engine(get_settings().database_url.get_secret_value())
    with eng.connect() as conn:
        users = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
        chunks = conn.execute(text("SELECT count(*) FROM chunks")).scalar_one()
    eng.dispose()
    return users, chunks


def test_retrieval_eval_offline_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "baseline.json"
    providers = Providers(
        HashEmbedder(),
        NoopReranker(),
        make_chat_model(get_settings()),
        ladder(),
        HeuristicAnalyst(),
    )
    before = _counts()
    rc = retrieval_eval.main(["--out", str(out)], providers=providers)
    assert rc == 0
    report = json.loads(out.read_text())
    for mode in ("vector", "hybrid"):
        m = report["modes"][mode]
        assert 0 <= m["recall_at_5"] <= 1
        assert 0 <= m["mrr"] <= 1
        assert m["n"] >= 10
    assert report["modes"]["hybrid+rerank"] == {"skipped": "reranker=none"}
    assert "| vector |" in capsys.readouterr().out
    assert _counts() == before  # the throwaway eval user and its rows are gone
