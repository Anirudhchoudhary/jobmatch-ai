from pathlib import Path

import pytest

from scripts.run_evals import main


@pytest.mark.usefixtures("eval_env")
def test_live_profile_fails_fast_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--profile", "live", "--out", str(tmp_path / "out")]) == 2
    assert "ANTHROPIC_API_KEY is required" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
