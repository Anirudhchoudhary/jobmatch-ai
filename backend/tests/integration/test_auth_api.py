import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from fastapi import Depends, FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api.deps import DEV_USER_ID, get_current_user_id, get_now
from app.auth.codes import hmac_hex
from app.auth.email import EmailSendError, get_email_sender
from app.core.settings import get_settings
from app.db.engine import get_engine, get_sessionmaker, make_engine
from app.db.models import LoginCode, User
from app.main import create_app

pytestmark = pytest.mark.integration

SIGNING = "test-signing-key-0123456789-abcdefghijkl"
OTP_KEY = "test-otp-hmac-key-0123456789-abcdefghijkl"
DOMAIN = "auth-test.example.com"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_code(self, to: str, code: str) -> None:
        if self.fail:
            raise EmailSendError("email send failed: status 500")
        self.sent.append((to, code))


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    client: httpx.AsyncClient
    sender: RecordingSender
    clock: Clock
    ip: str
    app: FastAPI
    extra_ips: list[str] = field(default_factory=list)

    def spoof_ip(self) -> str:
        ip = f"10.{uuid4().int % 250}.{uuid4().int % 250}.{uuid4().int % 250}"
        self.extra_ips.append(ip)
        return ip

    def email(self) -> str:
        return f"{uuid4().hex[:12]}@{DOMAIN}"

    async def request(self, email: str, xff: str | None = None) -> httpx.Response:
        headers = {"X-Forwarded-For": xff} if xff else {}
        return await self.client.post(
            "/api/auth/request-code", json={"email": email}, headers=headers
        )

    async def verify(self, email: str, code: str) -> httpx.Response:
        return await self.client.post("/api/auth/verify", json={"email": email, "code": code})

    async def sign_in(self, email: str) -> str:
        assert (await self.request(email)).status_code == 202
        resp = await self.verify(email, self.sender.sent[-1][1])
        assert resp.status_code == 200
        return resp.cookies["jm_session"]


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


def _configure(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("SESSION_SIGNING_KEY", SIGNING)
    monkeypatch.setenv("OTP_HMAC_KEY", OTP_KEY)
    if env == "prod":
        monkeypatch.setenv("EMAIL_SENDER", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
        monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _make_env(engine: AsyncEngine) -> AsyncIterator[Env]:
    app = create_app()
    sender = RecordingSender()
    clock = Clock(datetime.now(UTC))
    app.dependency_overrides[get_email_sender] = lambda: sender
    app.dependency_overrides[get_now] = clock

    @app.get("/api/_whoami")
    async def whoami(user_id: Annotated[UUID, Depends(get_current_user_id)]) -> dict[str, str]:
        return {"user_id": str(user_id)}

    ip = f"10.{uuid4().int % 250}.{uuid4().int % 250}.{uuid4().int % 250}"
    extra_ips: list[str] = []
    transport = httpx.ASGITransport(app=app, client=(ip, 12345))  # own socket-IP bucket per test
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Env(client, sender, clock, ip, app, extra_ips)
    finally:
        sm = async_sessionmaker(engine)
        async with sm() as s:
            key = get_settings().otp_hmac_key
            assert key is not None
            for address in (ip, *extra_ips):
                await s.execute(
                    text("DELETE FROM login_codes WHERE ip_hash = :h"),
                    {"h": hmac_hex(key, address)},
                )
            await s.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": f"%@{DOMAIN}"})
            await s.commit()


@pytest.fixture
async def env(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    _configure(monkeypatch, "local")
    async for e in _make_env(engine):
        yield e


@pytest.fixture
async def prod_env(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    _configure(monkeypatch, "prod")
    async for e in _make_env(engine):
        yield e


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def test_request_then_verify_sets_cookie_and_creates_user(
    env: Env, engine: AsyncEngine
) -> None:
    email = env.email()
    resp = await env.request(email)
    assert (resp.status_code, resp.json()) == (202, {"status": "sent"})
    assert env.sender.sent[0][0] == email
    code = env.sender.sent[0][1]
    assert re.fullmatch(r"\d{6}", code)
    resp = await env.verify(email, code)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "jm_session=" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert f"max-age={7 * 86400}" in cookie
    assert "secure" not in cookie  # local env keeps plain http://localhost working
    async with async_sessionmaker(engine)() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
    assert resp.json() == {"user_id": str(user.id)}
    assert user.last_seen_at is not None
    me = await env.client.get("/api/auth/me")
    assert me.json() == {"user_id": str(user.id), "email": email}


async def test_code_cannot_be_reused(env: Env) -> None:
    email = env.email()
    await env.request(email)
    code = env.sender.sent[-1][1]
    assert (await env.verify(email, code)).status_code == 200
    assert (await env.verify(email, code)).status_code == 400


async def test_email_variants_share_account(env: Env) -> None:
    base = env.email()
    await env.request(f"  {base.upper()} ")
    first = await env.verify(f" {base.upper()}  ", env.sender.sent[-1][1])
    await env.request(base)
    second = await env.verify(base, env.sender.sent[-1][1])
    assert first.status_code == second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]


async def test_new_request_invalidates_old_code(env: Env) -> None:
    email = env.email()
    await env.request(email)
    await env.request(email)
    old, new = env.sender.sent[0][1], env.sender.sent[1][1]
    if old != new:
        assert (await env.verify(email, old)).status_code == 400
    assert (await env.verify(email, new)).status_code == 200


async def test_five_wrong_attempts_lock_code(env: Env) -> None:
    email = env.email()
    await env.request(email)
    code = env.sender.sent[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert (await env.verify(email, wrong)).status_code == 400
    resp = await env.verify(email, code)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_code"


async def test_expired_code_rejected(env: Env) -> None:
    email = env.email()
    await env.request(email)
    env.clock.now += timedelta(minutes=11)
    assert (await env.verify(email, env.sender.sent[-1][1])).status_code == 400


async def test_per_email_limit(env: Env) -> None:
    email = env.email()
    for _ in range(3):
        assert (await env.request(email)).status_code == 202
    resp = await env.request(email)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "too_many_requests"
    env.clock.now += timedelta(hours=1, seconds=1)
    assert (await env.request(email)).status_code == 202


async def test_per_ip_limit(env: Env) -> None:
    for _ in range(10):
        assert (await env.request(env.email())).status_code == 202
    assert (await env.request(env.email())).status_code == 429


async def test_forwarded_for_uses_trusted_hop_not_spoofable_first_entry(prod_env: Env) -> None:
    env = prod_env
    real = env.spoof_ip()
    for i in range(10):  # the client rotates the leftmost entry; the proxy-appended one is fixed
        assert (await env.request(env.email(), xff=f"9.9.9.{i}, {real}")).status_code == 202
    assert (await env.request(env.email(), xff=f"9.9.9.99, {real}")).status_code == 429
    other = env.spoof_ip()
    assert (await env.request(env.email(), xff=f"9.9.9.1, {other}")).status_code == 202


async def test_request_context_source_ip_beats_rotating_forwarded_for(prod_env: Env) -> None:
    env = prod_env
    real = env.spoof_ip()
    context = json.dumps({"http": {"method": "POST", "sourceIp": real}})

    async def request(xff: str) -> httpx.Response:
        return await env.client.post(
            "/api/auth/request-code",
            json={"email": env.email()},
            headers={"X-Forwarded-For": xff, "x-amzn-request-context": context},
        )

    for i in range(10):  # every X-Forwarded-For entry rotates; the Lambda source IP does not
        assert (await request(f"9.9.9.{i}, 8.8.8.{i}")).status_code == 202
    assert (await request("9.9.9.99, 8.8.8.99")).status_code == 429


async def test_global_daily_email_cap(
    env: Env, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_sessionmaker(engine)() as s:
        recent = (
            await s.execute(
                select(func.count())
                .select_from(LoginCode)
                .where(LoginCode.created_at >= env.clock.now - timedelta(hours=24))
            )
        ).scalar_one()
    monkeypatch.setenv("OTP_GLOBAL_PER_DAY", str(recent + 2))
    get_settings.cache_clear()
    for _ in range(2):
        assert (await env.request(env.email())).status_code == 202
    resp = await env.request(env.email())
    assert resp.status_code == 429
    assert resp.json()["error"] == {
        "code": "too_many_requests",
        "message": "Sign-in is busy right now. Try again later.",
        "request_id": resp.headers["X-Request-ID"],
    }
    assert len(env.sender.sent) == 2
    env.clock.now += timedelta(hours=24, seconds=1)  # the window slides
    assert (await env.request(env.email())).status_code == 202


async def test_concurrent_requests_keep_one_live_code(env: Env, engine: AsyncEngine) -> None:
    email = env.email()
    results = await asyncio.gather(*(env.request(email) for _ in range(10)))
    codes = sorted(r.status_code for r in results)
    assert codes == [202] * 3 + [429] * 7
    key = get_settings().otp_hmac_key
    assert key is not None
    async with async_sessionmaker(engine)() as s:
        live = (
            await s.execute(
                select(func.count())
                .select_from(LoginCode)
                .where(
                    LoginCode.email_hash == hmac_hex(key, email),
                    LoginCode.consumed_at.is_(None),
                    LoginCode.invalidated_at.is_(None),
                )
            )
        ).scalar_one()
    assert live == 1


async def test_disposable_gets_same_response_and_no_email(env: Env) -> None:
    resp = await env.request("someone@mailinator.com")
    assert (resp.status_code, resp.json()) == (202, {"status": "sent"})
    assert env.sender.sent == []


async def test_unknown_and_known_email_same_response(env: Env) -> None:
    known = env.email()
    await env.sign_in(known)
    a = await env.request(known)
    b = await env.request(env.email())
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (202, {"status": "sent"})


async def test_send_failure_is_502_without_leaking(env: Env) -> None:
    env.sender.fail = True
    resp = await env.request(env.email())
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "email_failed"


async def test_invalid_email_rejected(env: Env) -> None:
    assert (await env.request("not-an-email")).status_code == 422


async def test_prod_requires_cookie(prod_env: Env) -> None:
    env = prod_env
    assert (await env.client.get("/api/_whoami")).status_code == 401
    email = env.email()
    cookie = await env.sign_in(email)
    # an unsigned/tampered cookie never authenticates, and never falls back to the dev user
    payload, sig = cookie.split(".")
    for bad in (f"{payload}.{sig[::-1]}", f"{payload[::-1]}.{sig}", "junk"):
        resp = await env.client.get("/api/_whoami", headers={"Cookie": f"jm_session={bad}"})
        assert resp.status_code == 401
    ok = await env.client.get("/api/_whoami", headers={"Cookie": f"jm_session={cookie}"})
    assert ok.status_code == 200
    assert ok.json()["user_id"] != str(DEV_USER_ID)
    me = await env.client.get("/api/auth/me", headers={"Cookie": f"jm_session={cookie}"})
    assert me.json()["email"] == email
    # an expired cookie is rejected
    env.clock.now += timedelta(days=8)
    expired = await env.client.get("/api/_whoami", headers={"Cookie": f"jm_session={cookie}"})
    assert expired.status_code == 401


async def test_cookie_signed_with_another_key_rejected(prod_env: Env) -> None:
    from pydantic import SecretStr

    from app.auth.session import issue

    cookie = await prod_env.sign_in(prod_env.email())
    uid = await prod_env.client.get("/api/auth/me", headers={"Cookie": f"jm_session={cookie}"})
    forged = issue(
        UUID(uid.json()["user_id"]),
        key=SecretStr("some-other-key-0123456789-abcdefghijklmn"),
        ttl=timedelta(days=7),
        now=prod_env.clock.now,
    )
    resp = await prod_env.client.get("/api/_whoami", headers={"Cookie": f"jm_session={forged}"})
    assert resp.status_code == 401


async def test_prod_cookie_is_secure(prod_env: Env) -> None:
    email = prod_env.email()
    await prod_env.request(email)
    resp = await prod_env.verify(email, prod_env.sender.sent[-1][1])
    assert "secure" in resp.headers["set-cookie"].lower()


async def test_local_without_cookie_uses_dev_user(env: Env) -> None:
    resp = await env.client.get("/api/_whoami")
    assert resp.json()["user_id"] == str(DEV_USER_ID)
    assert (await env.client.get("/api/auth/me")).status_code == 401


async def test_me_and_logout(env: Env) -> None:
    email = env.email()
    cookie = await env.sign_in(email)
    me = await env.client.get("/api/auth/me", headers={"Cookie": f"jm_session={cookie}"})
    assert me.status_code == 200 and me.json()["email"] == email
    out = await env.client.post("/api/auth/logout", json={})
    assert out.status_code == 204
    cleared = out.headers["set-cookie"].lower()
    assert "jm_session=" in cleared and "max-age=0" in cleared
    assert (await env.client.get("/api/auth/me", headers={"Cookie": ""})).status_code == 401


async def test_only_hashes_stored(env: Env, engine: AsyncEngine) -> None:
    email = env.email()
    await env.request(email)
    code = env.sender.sent[-1][1]
    async with async_sessionmaker(engine)() as s:
        row = (
            await s.execute(select(LoginCode).where(LoginCode.ip_hash == hmac_hex(_otp(), env.ip)))
        ).scalar_one()
    for value in (row.email_hash, row.ip_hash, row.code_hash):
        assert email not in value and env.ip not in value and code != value
        assert re.fullmatch(r"[0-9a-f]{64}", value)


def _otp() -> object:
    key = get_settings().otp_hmac_key
    assert key is not None
    return key


async def _flow(env: Env, email: str) -> None:
    await env.request(email)
    await env.verify(email, "000000")
    await env.verify(email, env.sender.sent[-1][1])
    env.sender.fail = True
    await env.request(env.email())
    await env.client.get("/api/auth/me")


async def test_email_never_logged(
    env: Env, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    email = env.email()
    await _flow(env, email)
    out = capsys.readouterr().out + caplog.text
    assert "request_completed" in out  # the rendered log pipeline really ran
    assert "login_code_send_failed" in out
    local, _, _ = email.partition("@")
    assert email not in out and local not in out and DOMAIN not in out
    assert env.ip not in out
    # also the raw structlog events, before any processor runs
    env.sender.fail = False
    other = env.email()
    with structlog.testing.capture_logs() as captured:
        await _flow(env, other)
    dumped = json.dumps(captured, default=str)
    assert other not in dumped and other.partition("@")[0] not in dumped
