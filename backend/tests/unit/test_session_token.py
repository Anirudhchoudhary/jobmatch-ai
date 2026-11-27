import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from app.auth import session

KEY = SecretStr("test-signing-key")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
TTL = timedelta(days=7)


def test_session_roundtrip() -> None:
    uid = uuid.uuid4()
    token = session.issue(uid, key=KEY, ttl=TTL, now=NOW)
    assert "=" not in token
    assert session.verify(token, key=KEY, now=NOW + timedelta(days=1)) == uid


def test_session_rejects_tampered_payload() -> None:
    token = session.issue(uuid.uuid4(), key=KEY, ttl=TTL, now=NOW)
    _, sig = token.split(".")
    forged = json.dumps({"uid": str(uuid.uuid4()), "exp": int((NOW + TTL).timestamp())})
    payload = base64.urlsafe_b64encode(forged.encode()).rstrip(b"=").decode()
    assert session.verify(f"{payload}.{sig}", key=KEY, now=NOW) is None


def test_session_rejects_wrong_key() -> None:
    token = session.issue(uuid.uuid4(), key=KEY, ttl=TTL, now=NOW)
    assert session.verify(token, key=SecretStr("other"), now=NOW) is None


def test_session_rejects_expired() -> None:
    token = session.issue(uuid.uuid4(), key=KEY, ttl=TTL, now=NOW)
    assert session.verify(token, key=KEY, now=NOW + TTL + timedelta(seconds=1)) is None


def test_session_rejects_garbage() -> None:
    for bad in ("", "a.b.c", "!!!.???", "abc", "....", "é.é"):
        assert session.verify(bad, key=KEY, now=NOW) is None
