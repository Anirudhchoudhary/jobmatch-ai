import pytest
from pydantic import SecretStr

from app.auth.codes import (
    codes_equal,
    hmac_hex,
    is_disposable,
    new_code,
    normalize_email,
)


def test_normalize_email_variants() -> None:
    assert normalize_email(" Jane.Doe@Example.COM ") == "jane.doe@example.com"
    for bad in ("no-at", "a@b", "a@@b.com", "@b.com", "a@.com", "a@b."):
        with pytest.raises(ValueError):
            normalize_email(bad)


def test_new_code_is_six_digits() -> None:
    for _ in range(100):
        code = new_code()
        assert len(code) == 6
        assert code.isdigit()


def test_hmac_hex_depends_on_key() -> None:
    a = hmac_hex(SecretStr("key-one"), "value")
    assert a == hmac_hex(SecretStr("key-one"), "value")
    assert a != hmac_hex(SecretStr("key-two"), "value")
    assert a != hmac_hex(SecretStr("key-one"), "other")
    assert len(a) == 64


def test_codes_equal() -> None:
    assert codes_equal("123456", "123456")
    assert not codes_equal("123456", "123457")


def test_disposable_includes_subdomains() -> None:
    assert is_disposable("x@mail.mailinator.com")
    assert is_disposable("x@yopmail.com")
    assert not is_disposable("x@example.com")
