"""Email senders for sign-in codes: Resend in deployed envs, console for local dev.

Senders never log or raise with the recipient address, the code (except the local
console sender) or the API key.
"""

from functools import lru_cache
from typing import Protocol

import httpx
import structlog
from pydantic import SecretStr

from app.core.settings import Settings, get_settings

log = structlog.get_logger(__name__)

RESEND_URL = "https://api.resend.com/emails"
SUBJECT = "Your JobMatch AI sign-in code"


class EmailSendError(Exception):
    """Raised when the email provider rejects or fails a send."""


class EmailSender(Protocol):
    async def send_code(self, to: str, code: str) -> None: ...


class ResendSender:
    def __init__(
        self,
        api_key: SecretStr,
        sender: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._client = client

    async def send_code(self, to: str, code: str) -> None:
        payload = {
            "from": self._sender,
            "to": [to],
            "subject": SUBJECT,
            "text": (
                f"Your code is {code}. It expires in 10 minutes. "
                "If you didn't ask for it, ignore this email."
            ),
        }
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        try:
            if self._client is not None:
                resp = await self._client.post(
                    RESEND_URL, json=payload, headers=headers, timeout=10.0
                )
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        RESEND_URL, json=payload, headers=headers, timeout=10.0
                    )
        except httpx.HTTPError as exc:
            # Exception text can embed the request URL/body details; keep only the type.
            raise EmailSendError(f"email send failed: {type(exc).__name__}") from None
        if not resp.is_success:
            raise EmailSendError(f"email send failed: status {resp.status_code}")


class ConsoleSender:
    """Local-dev only: logs the code (never the address) instead of sending."""

    def __init__(self, settings: Settings) -> None:
        if settings.env != "local":
            raise RuntimeError("ConsoleSender is only allowed when ENV=local")

    async def send_code(self, to: str, code: str) -> None:
        log.info("login_code_console", code=code)


@lru_cache
def get_email_sender() -> EmailSender:
    s = get_settings()
    if s.email_sender == "resend":
        if s.resend_api_key is None or not s.email_from:
            raise RuntimeError("RESEND_API_KEY and EMAIL_FROM are required for the resend sender")
        return ResendSender(s.resend_api_key, s.email_from)
    return ConsoleSender(s)
