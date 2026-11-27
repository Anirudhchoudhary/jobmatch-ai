"""scripts/secrets_set.py: prompts without echoing, generates signing keys, validates the DB URL."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError

from scripts.secrets_set import DatabaseUrlError, main, normalize_database_url


def _db_url(scheme: str, password: str, rest: str) -> str:
    """Fake test URLs, built at runtime so secret scanners don't read them as credentials."""
    return scheme + "://u:" + password + "@" + rest


GOOD_DB_URL = _db_url("postgresql+psycopg", "dbpass-PLANTED-1", "ep.neon.tech/db?sslmode=require")


class FakeSecretsClient:
    def __init__(self, current: dict[str, str] | None = None) -> None:
        self.current = current
        self.put: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        if self.current is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
                "GetSecretValue",
            )
        return {"SecretString": json.dumps(self.current)}

    def put_secret_value(self, **kwargs: Any) -> dict[str, str]:
        self.put.append(kwargs)
        return {}

    def create_secret(self, **kwargs: Any) -> dict[str, str]:
        self.created.append(kwargs)
        return {}

    def written(self) -> dict[str, str]:
        calls = self.put or self.created
        assert len(calls) == 1
        return dict(json.loads(calls[0]["SecretString"]))


def prompter(answers: dict[str, str]) -> Callable[[str], str]:
    """A getpass stand-in: answers by the key name at the start of the prompt (default Enter)."""

    def ask(prompt: str) -> str:
        key = prompt.split(" ", 1)[0].rstrip(":")
        return answers.get(key, "")

    return ask


def no_confirm(prompt: str) -> str:
    raise AssertionError(f"unexpected confirmation prompt: {prompt}")


FRESH_ANSWERS = {
    "ANTHROPIC_API_KEY": "sk-ant-PLANTED-anthropic",
    "DATABASE_URL": GOOD_DB_URL,
    "RESEND_API_KEY": "re_PLANTED_resend",
    "EMAIL_FROM": "JobMatch <login@mail.example.com>",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-PLANTED",
    "LANGFUSE_SECRET_KEY": "sk-lf-PLANTED",
}


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Iterator[Callable[..., tuple[int, str]]]:
    def _run(
        client: FakeSecretsClient,
        answers: dict[str, str],
        confirm: Callable[[str], str] = no_confirm,
    ) -> tuple[int, str]:
        code = main(
            ["--secret-id", "jobmatch/prod"],
            client=client,
            prompt=prompter(answers),
            confirm=confirm,
        )
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    yield _run


def test_secrets_set_never_prints_values(run: Callable[..., tuple[int, str]]) -> None:
    client = FakeSecretsClient(current=None)
    code, output = run(client, FRESH_ANSWERS)
    assert code == 0
    written = client.written()
    for key in FRESH_ANSWERS:
        assert key in output
    for value in written.values():
        assert value not in output
    assert "PLANTED" not in output
    # a missing secret is created, tagged, with put-equivalent content
    assert client.created[0]["Name"] == "jobmatch/prod"
    assert {"Key": "Project", "Value": "jobmatch-ai"} in client.created[0]["Tags"]


def test_secrets_set_generates_missing_keys(run: Callable[..., tuple[int, str]]) -> None:
    client = FakeSecretsClient(current=None)
    code, output = run(client, FRESH_ANSWERS)
    assert code == 0
    written = client.written()
    for key in ("SESSION_SIGNING_KEY", "OTP_HMAC_KEY"):
        assert len(written[key]) >= 32
        assert f"{key}: generated" in output
    assert written["SESSION_SIGNING_KEY"] != written["OTP_HMAC_KEY"]


def test_existing_values_kept_on_enter(run: Callable[..., tuple[int, str]]) -> None:
    current = {
        "ANTHROPIC_API_KEY": "sk-ant-old",
        "DATABASE_URL": GOOD_DB_URL,
        "RESEND_API_KEY": "re_old",
        "EMAIL_FROM": "login@mail.example.com",
        "SESSION_SIGNING_KEY": "s" * 64,
        "OTP_HMAC_KEY": "o" * 64,
    }
    client = FakeSecretsClient(current=dict(current))
    code, output = run(client, {"RESEND_API_KEY": "re_new"})
    assert code == 0
    assert client.created == []
    written = client.written()
    assert written == {**current, "RESEND_API_KEY": "re_new"}
    assert "ANTHROPIC_API_KEY: kept" in output
    assert "RESEND_API_KEY: set" in output
    assert "SESSION_SIGNING_KEY: kept" in output
    assert "LANGFUSE_PUBLIC_KEY" not in written


def test_short_existing_signing_key_is_regenerated(run: Callable[..., tuple[int, str]]) -> None:
    current = {**FRESH_ANSWERS, "SESSION_SIGNING_KEY": "short", "OTP_HMAC_KEY": "o" * 64}
    client = FakeSecretsClient(current=current)
    code, output = run(client, {})
    assert code == 0
    assert len(client.written()["SESSION_SIGNING_KEY"]) >= 32
    assert "SESSION_SIGNING_KEY: generated" in output


def test_missing_required_value_fails_without_writing(
    run: Callable[..., tuple[int, str]],
) -> None:
    client = FakeSecretsClient(current=None)
    answers = {k: v for k, v in FRESH_ANSWERS.items() if k != "RESEND_API_KEY"}
    code, output = run(client, answers)
    assert code == 1
    assert "RESEND_API_KEY" in output
    assert client.put == [] and client.created == []


def test_langfuse_keys_must_be_set_together(run: Callable[..., tuple[int, str]]) -> None:
    client = FakeSecretsClient(current=None)
    answers = {k: v for k, v in FRESH_ANSWERS.items() if k != "LANGFUSE_SECRET_KEY"}
    code, output = run(client, answers)
    assert code == 1
    assert "LANGFUSE" in output
    assert client.put == [] and client.created == []


def test_database_url_validation() -> None:
    assert normalize_database_url(GOOD_DB_URL, confirm=no_confirm) == GOOD_DB_URL
    plain = _db_url("postgresql", "p", "ep.neon.tech/db?sslmode=require")
    rewritten = normalize_database_url(plain, confirm=lambda _p: "")
    assert rewritten == _db_url("postgresql+psycopg", "p", "ep.neon.tech/db?sslmode=require")
    with pytest.raises(DatabaseUrlError, match="postgresql\\+psycopg://"):
        normalize_database_url(plain, confirm=lambda _p: "n")
    with pytest.raises(DatabaseUrlError, match="sslmode=require"):
        normalize_database_url(_db_url("postgresql+psycopg", "p", "h/db"), confirm=no_confirm)
    with pytest.raises(DatabaseUrlError, match="postgresql\\+psycopg://"):
        normalize_database_url("mysql://u:p@h/db?sslmode=require", confirm=no_confirm)


def test_database_url_errors_never_contain_the_url() -> None:
    with pytest.raises(DatabaseUrlError) as excinfo:
        normalize_database_url(
            _db_url("postgresql+psycopg", "dbpass-PLANTED", "h/db"), confirm=no_confirm
        )
    assert "PLANTED" not in str(excinfo.value)


def test_invalid_database_url_reprompts(run: Callable[..., tuple[int, str]]) -> None:
    attempts = iter([_db_url("postgresql+psycopg", "p", "h/db"), GOOD_DB_URL])
    base = prompter(FRESH_ANSWERS)

    def ask(prompt: str) -> str:
        return next(attempts) if prompt.startswith("DATABASE_URL") else base(prompt)

    client = FakeSecretsClient(current=None)
    code = main(["--secret-id", "jobmatch/prod"], client=client, prompt=ask, confirm=no_confirm)
    assert code == 0
    assert client.written()["DATABASE_URL"] == GOOD_DB_URL


def test_unreadable_secret_aborts(run: Callable[..., tuple[int, str]]) -> None:
    class Denied(FakeSecretsClient):
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue"
            )

    client = Denied()
    code, output = run(client, FRESH_ANSWERS)
    assert code == 1
    assert "AccessDeniedException" in output
    assert client.put == [] and client.created == []
