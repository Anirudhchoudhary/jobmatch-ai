import json

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.auth.email import ConsoleSender, EmailSendError, ResendSender
from app.core.settings import Settings

ADDRESS = "alice@example.com"
CODE = "123456"
KEY = "re_test_key_abc"
DB = "postgresql://u:p@localhost:5432/x"


def _sender(handler: httpx.MockTransport) -> ResendSender:
    return ResendSender(
        SecretStr(KEY),
        "JobMatch <noreply@example.org>",
        client=httpx.AsyncClient(transport=handler),
    )


async def test_resend_posts_expected_payload() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    await _sender(httpx.MockTransport(handle)).send_code(ADDRESS, CODE)

    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://api.resend.com/emails"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["from"] == "JobMatch <noreply@example.org>"
    assert body["to"] == [ADDRESS]
    assert body["subject"] == "Your JobMatch AI sign-in code"
    assert body["text"] == (
        f"Your code is {CODE}. It expires in 10 minutes. "
        "If you didn't ask for it, ignore this email."
    )


async def test_resend_error_hides_details() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"boom {ADDRESS} {CODE} {KEY}")

    with pytest.raises(EmailSendError) as exc:
        await _sender(httpx.MockTransport(handle)).send_code(ADDRESS, CODE)
    msg = str(exc.value)
    assert "500" in msg
    for secret in (ADDRESS, CODE, KEY):
        assert secret not in msg


def test_console_sender_refuses_outside_local() -> None:
    with pytest.raises(RuntimeError):
        ConsoleSender(Settings(env="ci", database_url=DB))


async def test_console_sender_never_logs_email() -> None:
    with capture_logs() as logs:
        await ConsoleSender(Settings(env="local", database_url=DB)).send_code(ADDRESS, CODE)
    (entry,) = [e for e in logs if e["event"] == "login_code_console"]
    assert entry["code"] == CODE
    assert ADDRESS not in json.dumps(logs)
