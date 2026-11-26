from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import settings as settings_module
from app.core.settings import Settings, get_settings
from app.db.engine import get_engine

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
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
