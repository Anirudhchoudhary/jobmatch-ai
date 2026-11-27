"""Email normalisation, OTP generation and hashing, and the disposable-domain denylist."""

import hashlib
import hmac
import secrets

from pydantic import SecretStr

DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "10minutemail.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "guerrillamail.org",
        "yopmail.com",
        "tempmail.com",
        "temp-mail.org",
        "throwawaymail.com",
        "trashmail.com",
        "sharklasers.com",
        "getnada.com",
        "maildrop.cc",
        "dispostable.com",
        "fakeinbox.com",
        "mintemail.com",
        "mailnesia.com",
        "spamgourmet.com",
        "tempinbox.com",
        "mohmal.com",
    }
)


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    parts = email.split("@")
    if len(parts) != 2:
        raise ValueError("invalid email")
    local, domain = parts
    labels = domain.split(".")
    if not local or len(labels) < 2 or not all(labels):
        raise ValueError("invalid email")
    return email


def hmac_hex(key: SecretStr, value: str) -> str:
    return hmac.new(key.get_secret_value().encode(), value.encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def codes_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def is_disposable(email: str) -> bool:
    labels = email.rsplit("@", 1)[-1].strip().lower().split(".")
    return any(".".join(labels[i:]) in DISPOSABLE_DOMAINS for i in range(len(labels)))
