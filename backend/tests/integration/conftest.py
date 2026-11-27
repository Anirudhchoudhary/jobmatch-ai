import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config

from app.core.settings import get_settings

LOCAL_DB_URL = "postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "db"}
OPT_IN_VAR = "JOBMATCH_ALLOW_NONLOCAL_DB"
BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def migrated() -> Iterator[None]:
    """Point at the local Compose DB (never prod/Neon by accident) and migrate it."""
    previous = os.environ.get("DATABASE_URL")
    if os.environ.get(OPT_IN_VAR) != "1":
        os.environ["DATABASE_URL"] = LOCAL_DB_URL
        host = urlparse(os.environ["DATABASE_URL"]).hostname
        assert host in ALLOWED_HOSTS, f"refusing non-local DB host {host!r}"
    get_settings.cache_clear()
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    yield
    get_settings.cache_clear()
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
