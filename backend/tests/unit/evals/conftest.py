import os
from collections.abc import Iterator

import pytest

from app.core.settings import get_settings
from app.guardrails.factory import get_guardrails
from app.llm.providers import get_providers
from evals import runner


def _clear_caches() -> None:
    for cached in (get_settings, get_providers, get_guardrails):
        cached.cache_clear()


@pytest.fixture
def eval_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """For tests that call `eval_settings`: it mutates os.environ and the caches by design."""
    saved = dict(os.environ)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch"
    )
    monkeypatch.setattr(runner, "_applied", None)
    _clear_caches()
    yield
    os.environ.clear()
    os.environ.update(saved)
    _clear_caches()
