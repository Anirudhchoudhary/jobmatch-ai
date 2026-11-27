"""Email OTP sign-in routes and the session cookie."""

import ipaddress
import json
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_now, get_session, get_session_token
from app.auth import service
from app.auth import session as session_token
from app.auth.email import EmailSender, get_email_sender
from app.core.errors import AppError
from app.core.origin_verify import origin_verified
from app.core.settings import Settings, get_settings

router = APIRouter(prefix="/api/auth")


class RequestCodeBody(BaseModel):
    email: str = Field(max_length=254)


class VerifyBody(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)


def _request_context_ip(request: Request) -> str | None:
    """`requestContext.http.sourceIp` of the Function URL event, if usable.

    Lambda Web Adapter serialises the invocation event's request context into this header on
    every request, replacing any value the client sent, so it is not client-controlled.
    """
    raw = request.headers.get("x-amzn-request-context")
    if not raw:
        return None
    try:
        source_ip = json.loads(raw)["http"]["sourceIp"]
        if not isinstance(source_ip, str):
            return None
        return str(ipaddress.ip_address(source_ip))
    except (ValueError, KeyError, TypeError):  # malformed: fall back, never a 500
        return None


def _viewer_address_ip(request: Request, settings: Settings) -> str | None:
    """IP part of CloudFront's `CloudFront-Viewer-Address` (`ip:port`, IPv6 without brackets).

    Trusted only when the request proved it came through CloudFront (origin token matches);
    otherwise the header is client-controlled.
    """
    token = settings.origin_verify_token
    if token is None or not origin_verified(
        list(request.scope["headers"]), token.get_secret_value()
    ):
        return None
    raw = request.headers.get("cloudfront-viewer-address", "").strip()
    host = raw.rpartition(":")[0].removeprefix("[").removesuffix("]")  # port is after last colon
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:  # malformed or missing: fall back, never a 500
        return None


def client_ip(request: Request, settings: Settings) -> str:
    """Caller address for rate limiting.

    Outside local, behind a verified CloudFront request the viewer address is used. Otherwise
    the source IP Lambda recorded for the Function URL request is used. Failing
    that, the leftmost X-Forwarded-For entry is client-controlled, so the entry that the last
    `trusted_proxy_hops` proxies appended is used (rightmost by default). Without either (or in
    local) it is the socket peer; if even that is missing, all such callers share the single
    "unknown" bucket, which fails closed against the per-IP limit.
    """
    if settings.env != "local":
        if viewer_ip := _viewer_address_ip(request, settings):
            return viewer_ip
        if source_ip := _request_context_ip(request):
            return source_ip
        entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",")]
        entries = [e for e in entries if e]
        if entries:
            return entries[max(len(entries) - settings.trusted_proxy_hops, 0)]
    return request.client.host if request.client else "unknown"


def _cookie_args(settings: Settings) -> dict[str, object]:
    return {
        "key": settings.session_cookie_name,
        "httponly": True,
        "samesite": "lax",
        "secure": settings.env != "local",  # http://localhost must keep working in dev
        "path": "/",
    }


@router.post("/request-code", status_code=202)
async def post_request_code(
    body: RequestCodeBody,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
    now: Annotated[datetime, Depends(get_now)],
) -> dict[str, str]:
    await service.request_code(
        session,
        email=body.email,
        ip=client_ip(request, settings),
        settings=settings,
        sender=sender,
        now=now,
    )
    return {"status": "sent"}


@router.post("/verify")
async def post_verify(
    body: VerifyBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_now)],
) -> dict[str, str]:
    if settings.session_signing_key is None:  # before the code is consumed
        raise AppError("auth_unavailable", "Sign-in is not available right now.", 503)
    user_id = await service.verify_code(
        session, email=body.email, code=body.code, settings=settings, now=now
    )
    ttl = timedelta(days=settings.session_ttl_days)
    token = session_token.issue(user_id, key=settings.session_signing_key, ttl=ttl, now=now)
    response.set_cookie(value=token, max_age=int(ttl.total_seconds()), **_cookie_args(settings))  # type: ignore[arg-type]
    return {"user_id": str(user_id)}


@router.post("/logout", status_code=204)
async def post_logout(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    response = Response(status_code=204)
    response.delete_cookie(**_cookie_args(settings))  # type: ignore[arg-type]
    return response


@router.get("/me")
async def get_me(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(get_session_token)],
    now: Annotated[datetime, Depends(get_now)],
) -> dict[str, str | None]:
    user = await service.user_from_token(session, token, settings=settings, now=now)
    if user is None:
        raise AppError("auth_required", "Sign in to continue.", 401)
    return {"user_id": str(user.id), "email": user.email}
