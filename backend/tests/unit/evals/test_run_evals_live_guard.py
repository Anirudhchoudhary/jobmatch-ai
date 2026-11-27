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


@pytest.mark.usefixtures("eval_env")
def test_missing_baseline_fails_before_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom(*_a: object) -> None:
        raise AssertionError("run started")

    monkeypatch.setattr("scripts.run_evals._run", boom)
    args = ["--profile", "offline", "--baseline", str(tmp_path / "nope.json")]
    assert main([*args, "--out", str(tmp_path / "out")]) == 2
    assert "cannot read baseline" in capsys.readouterr().err
