"""Set the prod secret JSON interactively (run via `make secrets-set`). Never echoes a value.

Reads the current secret if there is one; prompts for each key with getpass (Enter keeps the
current value); generates the signing keys when absent; writes with PutSecretValue, creating
the secret (tagged Project=jobmatch-ai) when it does not exist yet. Prints key names and
set/kept/generated only.
"""

import argparse
import getpass
import json
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

REQUIRED_KEYS = ("ANTHROPIC_API_KEY", "DATABASE_URL", "RESEND_API_KEY", "EMAIL_FROM")
OPTIONAL_PAIR = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
GENERATED_KEYS = ("SESSION_SIGNING_KEY", "OTP_HMAC_KEY")
MIN_GENERATED_LEN = 32
MAX_ATTEMPTS = 3
DB_SCHEME = "postgresql+psycopg://"
PLAIN_SCHEME = "postgresql://"
TAGS = [{"Key": "Project", "Value": "jobmatch-ai"}]

Prompt = Callable[[str], str]


class DatabaseUrlError(ValueError):
    """An unusable DATABASE_URL. The message never contains the URL (it holds a password)."""


class SetupError(RuntimeError):
    """Aborts without writing anything. The message never contains a value."""


def normalize_database_url(url: str, *, confirm: Prompt) -> str:
    if url.startswith(PLAIN_SCHEME):
        answer = confirm(f"DATABASE_URL uses {PLAIN_SCHEME}; rewrite to {DB_SCHEME}? [Y/n] ")
        if answer.strip().lower() not in ("", "y", "yes"):
            raise DatabaseUrlError(f"DATABASE_URL must start with {DB_SCHEME}")
        url = DB_SCHEME + url.removeprefix(PLAIN_SCHEME)
    if not url.startswith(DB_SCHEME):
        raise DatabaseUrlError(f"DATABASE_URL must start with {DB_SCHEME}")
    if "sslmode=require" not in url:
        raise DatabaseUrlError("DATABASE_URL must contain sslmode=require")
    return url


def _read_current(client: Any, secret_id: str) -> dict[str, str] | None:
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code == "ResourceNotFoundException":
            return None
        raise SetupError(f"could not read secret {secret_id!r}: {code}") from None
    except BotoCoreError as exc:
        raise SetupError(f"could not read secret {secret_id!r}: {type(exc).__name__}") from None
    try:
        parsed = json.loads(response.get("SecretString", ""))
    except json.JSONDecodeError:
        raise SetupError(f"secret {secret_id!r} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise SetupError(f"secret {secret_id!r} must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def _ask(key: str, hint: str, prompt: Prompt) -> str:
    suffix = f" [Enter {hint}]" if hint else ""
    return prompt(f"{key}{suffix}: ").strip()


def _required(key: str, current: str | None, prompt: Prompt, confirm: Prompt) -> tuple[str, str]:
    for _ in range(MAX_ATTEMPTS):
        entered = _ask(key, "keeps current" if current else "", prompt)
        value, status = (entered, "set") if entered else (current, "kept")
        if not value:
            print(f"{key} is required")
            continue
        if key == "DATABASE_URL":
            try:
                value = normalize_database_url(value, confirm=confirm)
            except DatabaseUrlError as exc:
                print(f"{exc}; enter a new value")
                current = None  # an invalid stored URL cannot be kept
                continue
        return value, status
    raise SetupError(f"{key}: no valid value after {MAX_ATTEMPTS} attempts")


def _generated(key: str, current: str | None, prompt: Prompt) -> tuple[str, str]:
    usable = current if current and len(current) >= MIN_GENERATED_LEN else None
    for _ in range(MAX_ATTEMPTS):
        entered = _ask(key, "keeps current" if usable else "generates one", prompt)
        if not entered:
            if usable:
                return usable, "kept"
            return secrets.token_urlsafe(48), "generated"
        if len(entered) >= MIN_GENERATED_LEN:
            return entered, "set"
        print(f"{key} must be at least {MIN_GENERATED_LEN} characters")
    raise SetupError(f"{key}: no valid value after {MAX_ATTEMPTS} attempts")


def collect(current: dict[str, str], prompt: Prompt, confirm: Prompt) -> dict[str, str]:
    values = dict(current)
    statuses: list[tuple[str, str]] = []
    for key in REQUIRED_KEYS:
        values[key], status = _required(key, current.get(key), prompt, confirm)
        statuses.append((key, status))
    for key in OPTIONAL_PAIR:
        hint = "keeps current" if current.get(key) else "skips"
        entered = _ask(key, hint, prompt)
        if entered:
            values[key] = entered
            statuses.append((key, "set"))
        elif key in current:
            statuses.append((key, "kept"))
        else:
            statuses.append((key, "unset"))
    if (OPTIONAL_PAIR[0] in values) != (OPTIONAL_PAIR[1] in values):
        raise SetupError(f"{' and '.join(OPTIONAL_PAIR)} must be set together or not at all")
    for key in GENERATED_KEYS:
        values[key], status = _generated(key, current.get(key), prompt)
        statuses.append((key, status))
    for key, status in statuses:
        print(f"{key}: {status}")
    return values


def main(
    argv: list[str] | None = None,
    *,
    client: Any = None,
    prompt: Prompt = getpass.getpass,
    confirm: Prompt = input,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args(argv)
    if client is None:
        import boto3

        client = boto3.client("secretsmanager", region_name=args.region)
    try:
        current = _read_current(client, args.secret_id)
        values = collect(current or {}, prompt, confirm)
        payload = json.dumps(values)
        if current is None:
            client.create_secret(
                Name=args.secret_id,
                Description="JobMatch AI prod settings (managed by make secrets-set)",
                SecretString=payload,
                Tags=TAGS,
            )
            print(f"created secret {args.secret_id} with {len(values)} keys")
        else:
            client.put_secret_value(SecretId=args.secret_id, SecretString=payload)
            print(f"updated secret {args.secret_id} with {len(values)} keys")
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        print(f"error: could not write secret {args.secret_id!r}: {code}", file=sys.stderr)
        return 1
    except BotoCoreError as exc:
        print(f"error: could not write secret: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
