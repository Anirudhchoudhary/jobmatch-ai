from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import settings as settings_module
from app.core.settings import get_settings


def test_repo_env_file_does_not_leak_into_unit_tests() -> None:
    repo_env = Path(settings_module.__file__).resolve().parents[3] / ".env"
    assert repo_env != settings_module.ENV_FILE
    assert settings_module.ENV_FILE.read_text() == ""


def test_decoy_env_file_with_database_url_is_ignored(tmp_path: Path) -> None:
    # A real repo-root .env would look like this; the fixture must not read it.
    decoy_dir = tmp_path / "repo"
    decoy_dir.mkdir()
    (decoy_dir / ".env").write_text('DATABASE_URL="postgresql+psycopg://u:p@neon.example/db"\n')
    with pytest.raises(ValidationError):
        get_settings()


def test_unit_tests_see_only_env_vars_not_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.ENV_FILE.write_text('DATABASE_URL="postgresql+psycopg://u:p@from-file/db"\n')
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@from-env/db")
    get_settings.cache_clear()
    assert "from-env" in get_settings().database_url.get_secret_value()
