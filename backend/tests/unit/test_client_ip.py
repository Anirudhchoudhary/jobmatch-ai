"""Which address the sign-in rate limit counts against."""

import json

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.api.auth import client_ip
from app.core.settings import Settings

DB = "postgresql+asyncpg://u:p@localhost/db"
PEER = "172.16.0.9"


def _settings(env: str = "ci", **kw: object) -> Settings:
    return Settings(_env_file=None, database_url=DB, env=env, **kw)  # type: ignore[arg-type]


def _request(headers: dict[str, str], peer: str | None = PEER) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/request-code",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def _context(source_ip: object) -> str:
    # The shape Lambda Web Adapter forwards for a Function URL (API Gateway v2 payload) event.
    return json.dumps({"accountId": "anonymous", "http": {"method": "POST", "sourceIp": source_ip}})


def test_request_context_source_ip_wins_over_forwarded_for() -> None:
    req = _request(
        {"x-amzn-request-context": _context("203.0.113.7"), "x-forwarded-for": "1.1.1.1"}
    )
    assert client_ip(req, _settings()) == "203.0.113.7"


@pytest.mark.parametrize(
    "header",
    [
        "{not json",
        json.dumps(["a", "list"]),
        json.dumps({"http": "nope"}),
        json.dumps({"http": {}}),
        _context(""),
        _context(12345),
        _context("not-an-ip"),
        json.dumps({"identity": {"sourceIp": "203.0.113.9"}}),  # not the Function URL shape
    ],
)
def test_unusable_request_context_falls_back_to_forwarded_for(header: str) -> None:
    req = _request({"x-amzn-request-context": header, "x-forwarded-for": "9.9.9.9, 198.51.100.4"})
    assert client_ip(req, _settings()) == "198.51.100.4"


def test_unusable_request_context_without_forwarded_for_uses_peer() -> None:
    req = _request({"x-amzn-request-context": "{not json"})
    assert client_ip(req, _settings()) == PEER


def test_forwarded_for_uses_trusted_hop() -> None:
    req = _request({"x-forwarded-for": "9.9.9.9, 198.51.100.4, 198.51.100.5"})
    assert client_ip(req, _settings()) == "198.51.100.5"
    assert client_ip(req, _settings(trusted_proxy_hops=2)) == "198.51.100.4"


def test_local_ignores_both_headers() -> None:
    req = _request(
        {"x-amzn-request-context": _context("203.0.113.7"), "x-forwarded-for": "1.1.1.1"}
    )
    assert client_ip(req, _settings(env="local")) == PEER


def test_no_peer_shares_the_unknown_bucket() -> None:
    assert client_ip(_request({}, peer=None), _settings()) == "unknown"


def test_trusted_proxy_hops_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _settings(trusted_proxy_hops=0)


TOKEN = "t" * 40


def _cf(viewer: str, token: str | None = TOKEN) -> Request:
    headers = {
        "cloudfront-viewer-address": viewer,
        "x-forwarded-for": "9.9.9.9, 198.51.100.4",
    }
    if token is not None:
        headers["x-origin-verify"] = token
    return _request(headers)


@pytest.mark.parametrize(
    ("viewer", "expected"),
    [
        ("203.0.113.10:46532", "203.0.113.10"),
        ("2001:db8::1:46532", "2001:db8::1"),
        ("[2001:db8::1]:46532", "2001:db8::1"),
    ],
)
def test_client_ip_from_viewer_address_ipv4_and_ipv6(viewer: str, expected: str) -> None:
    assert client_ip(_cf(viewer), _settings(origin_verify_token=TOKEN)) == expected


@pytest.mark.parametrize("viewer", ["", "garbage", "203.0.113.10", "999.1.1.1:80", "abc:123"])
def test_malformed_viewer_address_falls_back(viewer: str) -> None:
    assert client_ip(_cf(viewer), _settings(origin_verify_token=TOKEN)) == "198.51.100.4"


def test_viewer_address_ignored_without_verified_token() -> None:
    settings = _settings(origin_verify_token=TOKEN)
    assert client_ip(_cf("203.0.113.10:1", token="bad"), settings) == "198.51.100.4"
    assert client_ip(_cf("203.0.113.10:1", token=None), settings) == "198.51.100.4"
    assert client_ip(_cf("203.0.113.10:1"), _settings()) == "198.51.100.4"
