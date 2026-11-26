"""Application settings; secrets come from env/.env or AWS Secrets Manager."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import load_secret

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class _Bootstrap(BaseSettings):
    """Reads only the fields needed to decide where the real settings come from."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    aws_region: str = "us-west-2"
    secrets_source: Literal["env", "aws"] = "env"
    secret_id: str = "jobmatch/dev"  # noqa: S105  (an id, not a secret)


class Settings(_Bootstrap):
    env: Literal["local", "ci", "dev", "prod"] = "local"
    database_url: SecretStr
    log_level: str = "INFO"
    db_connect_timeout_s: float = 3.0


@lru_cache
def get_settings() -> Settings:
    bootstrap = _Bootstrap(_env_file=ENV_FILE)
    overrides: dict[str, Any] = {}
    if bootstrap.secrets_source == "aws":
        secret = load_secret(bootstrap.secret_id, region=bootstrap.aws_region)
        overrides = {k.lower(): v for k, v in secret.items()}
    return Settings(_env_file=ENV_FILE, **overrides)
