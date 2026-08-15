from __future__ import annotations

from datetime import datetime, timezone

import jwt
import pytest

from backend.app.config import get_settings
from backend.app.security import tokens


def _configure_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unit:user@localhost/unit_test")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_TEST_FIXTURES", "true")
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-value-with-32-characters-minimum")
    monkeypatch.setenv("JWT_ACCESS_TOKEN_MINUTES", "15")
    monkeypatch.setenv("JWT_REFRESH_TOKEN_DAYS", "7")
    get_settings.cache_clear()


def test_create_access_token_contains_required_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_test_settings(monkeypatch)

    encoded = tokens.create_access_token("user-1", ["customer", "partner"])
    payload = tokens.decode_token(encoded)

    assert payload["sub"] == "user-1"
    assert payload["roles"] == ["customer", "partner"]
    assert payload["type"] == "access"
    assert payload["jti"]
    assert datetime.fromtimestamp(payload["exp"], tz=timezone.utc) > datetime.now(timezone.utc)


def test_decode_token_rejects_tampered_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_test_settings(monkeypatch)

    encoded = tokens.create_access_token("user-1", ["customer"])
    header, payload, signature = encoded.split(".")
    replacement = "A" if not signature.startswith("A") else "B"
    tampered = f"{header}.{payload}.{replacement}{signature[1:]}"

    with pytest.raises(jwt.InvalidTokenError):
        tokens.decode_token(tampered)


def test_decode_token_rejects_alg_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_test_settings(monkeypatch)
    unsafe = jwt.encode(
        {"sub": "user-1", "roles": ["admin"], "type": "access"},
        key="",
        algorithm="none",
    )

    with pytest.raises(jwt.InvalidTokenError):
        tokens.decode_token(unsafe)


def test_refresh_token_returns_raw_digest_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_test_settings(monkeypatch)

    raw, digest, expires_at = tokens.create_refresh_token()

    assert len(raw) > 40
    assert digest == tokens.token_hash(raw)
    assert raw != digest
    assert expires_at > datetime.now(timezone.utc)
    assert tokens.token_hash(raw) == tokens.token_hash(raw)
