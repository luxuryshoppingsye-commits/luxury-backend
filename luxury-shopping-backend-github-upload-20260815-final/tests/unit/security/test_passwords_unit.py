from __future__ import annotations

import hashlib

import pytest

from backend.app.security.passwords import hash_password, validate_password, verify_password


def test_hash_password_uses_salted_argon2_and_never_returns_plaintext() -> None:
    password = "ValidPass123"

    first = hash_password(password)
    second = hash_password(password)

    assert first.startswith("$argon2id$")
    assert second.startswith("$argon2id$")
    assert first != second
    assert password not in first
    assert verify_password(password, first) == (True, False)
    assert verify_password("WrongPass123", first) == (False, False)


def test_verify_password_handles_invalid_hash_without_leaking_exception() -> None:
    assert verify_password("ValidPass123", "not-a-valid-hash") == (False, False)
    assert verify_password("ValidPass123", "$argon2id$broken") == (False, False)


def test_verify_password_supports_legacy_pbkdf2_and_requests_rehash() -> None:
    password = "LegacyPass123"
    salt = "fixed-test-salt"
    encoded = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()

    assert verify_password(password, encoded, legacy_salt=salt) == (True, True)
    assert verify_password("WrongPass123", encoded, legacy_salt=salt) == (False, True)


@pytest.mark.parametrize("password", ["short7", "NoDigitsHere", "12345678"])
def test_validate_password_rejects_weak_values(password: str) -> None:
    with pytest.raises(ValueError):
        validate_password(password)


def test_validate_password_accepts_unicode_letters_and_digits() -> None:
    validate_password("كلمةمرور123")
