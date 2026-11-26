from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import pii
from app.core import settings as settings_module
from app.core.settings import Settings, get_settings
from app.db.engine import get_engine
from app.llm.providers import get_providers

_ISOLATED_ENV_VARS = sorted({name.upper() for name in Settings.model_fields} | {"SECRET_ID"})


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Unit tests are offline: no real repo-root .env, no ambient settings env vars."""
    for key in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    monkeypatch.setattr(settings_module, "ENV_FILE", empty)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_providers.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_providers.cache_clear()


@pytest.fixture
def presidio_engine() -> Iterator[None]:
    """Opt a test into the Presidio engine; restore the process default afterwards."""
    pii.set_pii_engine("presidio")
    yield
    pii.set_pii_engine("regex")


@pytest.fixture(autouse=True)
def _reset_pii_engine() -> Iterator[None]:
    yield
    pii.set_pii_engine("regex")
