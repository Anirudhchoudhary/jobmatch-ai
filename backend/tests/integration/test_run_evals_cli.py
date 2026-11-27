import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.settings import get_settings
from app.db.engine import get_engine, get_sessionmaker
from app.guardrails.factory import get_guardrails
from app.llm.providers import get_providers
from evals import runner
from scripts.run_evals import main

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _restore_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = dict(os.environ)
    monkeypatch.setattr(runner, "_applied", None)
    yield
    os.environ.clear()
    os.environ.update(saved)
    for cached in (get_settings, get_providers, get_guardrails, get_engine, get_sessionmaker):
        cached.cache_clear()


def test_cli_offline_ci_subset_writes_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--profile",
            "offline",
            "--variant",
            "system",
            "--subset",
            "ci",
            "--out",
            str(tmp_path),
            "--no-gate",
        ]
    )
    assert code == 0
    assert (tmp_path / "report.md").exists()
    data = json.loads((tmp_path / "report.json").read_text())
    assert len(data["cases"]) == 11
    assert "judge_cost_usd" not in data["summary"]
    assert "## Gate" in capsys.readouterr().out


def test_cli_write_baseline_and_gate_failure(tmp_path: Path) -> None:
    base = tmp_path / "b" / "baseline.json"
    thresholds = tmp_path / "t.yaml"
    thresholds.write_text("offline:\n  skill_f1: {min: 2.0}\n")
    code = main(
        [
            "--profile",
            "offline",
            "--variant",
            "system",
            "--subset",
            "ci",
            "--out",
            str(tmp_path),
            "--write-baseline",
            str(base),
            "--thresholds",
            str(thresholds),
        ]
    )
    assert code == 1
    assert set(json.loads(base.read_text())) == {"meta", "summary"}
