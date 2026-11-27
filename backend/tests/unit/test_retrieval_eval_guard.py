from pathlib import Path

import pytest

from app.core.settings import get_settings
from app.llm.chat import make_chat_model
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers
from app.llm.rerank import NoopReranker
from scripts import retrieval_eval


def test_refuses_to_write_committed_baseline_with_offline_embedder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
    providers = Providers(HashEmbedder(), NoopReranker(), make_chat_model(get_settings()))
    out = retrieval_eval.REPO_ROOT / "evals" / "zqx_guard_test.json"

    def no_run(*_: object, **__: object) -> None:
        raise AssertionError("the eval must not run")

    monkeypatch.setattr(retrieval_eval, "_run", no_run)
    with pytest.raises(SystemExit) as exc:
        retrieval_eval.main(["--out", str(out)], providers=providers)
    assert exc.value.code not in (0, None)
    assert "EMBEDDER_PROVIDER=bedrock" in capsys.readouterr().err
    assert not out.exists()
    # A relative path that resolves under evals/ is refused too.
    monkeypatch.chdir(retrieval_eval.REPO_ROOT / "backend")
    with pytest.raises(SystemExit):
        retrieval_eval.main(["--out", "../evals/zqx_guard_test.json"], providers=providers)


def test_offline_output_outside_evals_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
    providers = Providers(HashEmbedder(), NoopReranker(), make_chat_model(get_settings()))

    async def fake_run(*_: object) -> dict[str, object]:
        return {"providers": {}, "modes": {}}

    monkeypatch.setattr(retrieval_eval, "_run", fake_run)
    out = tmp_path / "baseline.json"
    assert retrieval_eval.main(["--out", str(out)], providers=providers) == 0
    assert out.exists()
