"""Stateless signed session tokens: base64url(json payload) + "." + base64url(HMAC-SHA256)."""

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta

from pydantic import SecretStr


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii") + b"=" * (-len(text) % 4))


def _sign(payload_b64: str, key: SecretStr) -> bytes:
    return hmac.new(
        key.get_secret_value().encode(), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()


def issue(user_id: uuid.UUID, *, key: SecretStr, ttl: timedelta, now: datetime) -> str:
    payload = json.dumps(
        {"uid": str(user_id), "exp": int((now + ttl).timestamp())}, separators=(",", ":")
    )
    payload_b64 = _b64(payload.encode())
    return f"{payload_b64}.{_b64(_sign(payload_b64, key))}"


def verify(token: str, *, key: SecretStr, now: datetime) -> uuid.UUID | None:
    try:
        payload_b64, sig_b64 = token.split(".")
        if not hmac.compare_digest(_unb64(sig_b64), _sign(payload_b64, key)):
            return None
        data = json.loads(_unb64(payload_b64))
        if int(data["exp"]) <= int(now.timestamp()):
            return None
        return uuid.UUID(data["uid"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        return None
