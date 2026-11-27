"""Email OTP sign-in: issuing codes (rate limited) and exchanging them for a user id.

Only HMAC digests of the email, IP and code are stored. Nothing here logs an address.
"""

from datetime import datetime, timedelta
from uuid import UUID

import structlog
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import session as session_token
from app.auth.codes import codes_equal, hmac_hex, is_disposable, new_code, normalize_email
from app.auth.email import EmailSender, EmailSendError
from app.core.errors import AppError
from app.core.settings import Settings
from app.db.models import LoginCode, User

log = structlog.get_logger(__name__)

_WINDOW = timedelta(hours=1)
_GLOBAL_WINDOW = timedelta(hours=24)
_GLOBAL_LOCK = "0" * 8  # the single key in the global namespace


def _invalid_code() -> AppError:
    return AppError("invalid_code", "That code is invalid or expired.", 400)


def _otp_key(settings: Settings) -> SecretStr:
    if settings.otp_hmac_key is None:
        raise AppError("auth_unavailable", "Sign-in is not available right now.", 503)
    return settings.otp_hmac_key


def _code_hash(key: SecretStr, email_hash: str, code: str) -> str:
    return hmac_hex(key, f"{email_hash}:{code}")


async def _count_since(
    session: AsyncSession, since: datetime, column: object = None, value: str | None = None
) -> int:
    """Login codes created since `since`, for one email / IP hash or (no column) in total."""
    stmt = select(func.count()).select_from(LoginCode).where(LoginCode.created_at >= since)
    if column is not None:
        stmt = stmt.where(column == value)  # type: ignore[arg-type]
    return (await session.execute(stmt)).scalar_one()


async def _lock(session: AsyncSession, namespace: int, digest: str) -> None:
    """Transaction-scoped advisory lock so concurrent requests serialise per email / per IP."""
    key = int.from_bytes(bytes.fromhex(digest[:8]), "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": namespace, "k": key}
    )


async def request_code(
    session: AsyncSession,
    *,
    email: str,
    ip: str,
    settings: Settings,
    sender: EmailSender,
    now: datetime,
) -> None:
    try:
        address = normalize_email(email)
    except ValueError:
        raise AppError("invalid_email", "Enter a valid email address.", 422) from None
    if is_disposable(address):
        return  # same response as a real send; nothing is stored or mailed
    key = _otp_key(settings)
    email_hash = hmac_hex(key, address)
    ip_hash = hmac_hex(key, ip)
    since = now - _WINDOW
    await _lock(session, 3, _GLOBAL_LOCK)  # always global first, then email, then ip
    await _lock(session, 1, email_hash)
    await _lock(session, 2, ip_hash)
    if await _count_since(session, now - _GLOBAL_WINDOW) >= settings.otp_global_per_day:
        log.warning("login_code_global_cap_reached")
        raise AppError("too_many_requests", "Sign-in is busy right now. Try again later.", 429)
    too_many = AppError("too_many_requests", "Too many codes requested. Try again in an hour.", 429)
    if await _count_since(session, since, LoginCode.email_hash, email_hash) >= (
        settings.otp_per_email_per_hour
    ):
        raise too_many
    if await _count_since(session, since, LoginCode.ip_hash, ip_hash) >= (
        settings.otp_per_ip_per_hour
    ):
        raise too_many

    await session.execute(
        update(LoginCode)
        .where(
            LoginCode.email_hash == email_hash,
            LoginCode.consumed_at.is_(None),
            LoginCode.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    code = new_code()
    session.add(
        LoginCode(
            email_hash=email_hash,
            ip_hash=ip_hash,
            code_hash=_code_hash(key, email_hash, code),
            expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
            created_at=now,
        )
    )
    await session.commit()
    try:
        await sender.send_code(address, code)
    except EmailSendError as exc:
        log.warning("login_code_send_failed", error_type=type(exc).__name__)
        raise AppError(
            "email_failed", "We couldn't send the email. Try again shortly.", 502
        ) from None


async def verify_code(
    session: AsyncSession, *, email: str, code: str, settings: Settings, now: datetime
) -> UUID:
    try:
        address = normalize_email(email)
    except ValueError:
        raise _invalid_code() from None
    key = _otp_key(settings)
    email_hash = hmac_hex(key, address)
    row = (
        await session.execute(
            select(LoginCode)
            .where(
                LoginCode.email_hash == email_hash,
                LoginCode.consumed_at.is_(None),
                LoginCode.invalidated_at.is_(None),
                LoginCode.expires_at > now,
            )
            .order_by(LoginCode.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.attempts >= settings.otp_max_attempts:
        await session.rollback()
        raise _invalid_code()
    row.attempts += 1  # counted before comparing, and kept even when the guess is wrong
    if not codes_equal(row.code_hash, _code_hash(key, email_hash, code)):
        await session.commit()
        raise _invalid_code()
    row.consumed_at = now
    user_id = (
        await session.execute(
            insert(User)
            .values(email=address, last_seen_at=now)
            .on_conflict_do_update(index_elements=["email"], set_={"last_seen_at": now})
            .returning(User.id)
        )
    ).scalar_one()
    await session.commit()
    return user_id


async def user_from_token(
    session: AsyncSession, token: str | None, *, settings: Settings, now: datetime
) -> User | None:
    """The user a valid, unexpired session token names (and who still exists), else None."""
    if not token or settings.session_signing_key is None:
        return None
    user_id = session_token.verify(token, key=settings.session_signing_key, now=now)
    if user_id is None:
        return None
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
