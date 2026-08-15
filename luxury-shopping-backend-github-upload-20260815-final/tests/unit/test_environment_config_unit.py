from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.config import Settings


def _settings(**overrides: object) -> Settings:
    values = {
        "DATABASE_URL": "postgresql://user@127.0.0.1:55432/luxury_test",
        "APP_ENV": "test",
        "ALLOW_TEST_FIXTURES": True,
        "JWT_SECRET": "unit-test-jwt-secret-512512512512512512",
    }
    values.update(overrides)
    return Settings(**values)


def test_test_environment_requires_trusted_test_database_name() -> None:
    with pytest.raises(ValidationError, match="trusted test database"):
        _settings(DATABASE_URL="postgresql://user@127.0.0.1:55432/luxury_development")


def test_test_database_is_rejected_outside_test_environment() -> None:
    with pytest.raises(ValidationError, match="not allowed outside APP_ENV=test"):
        _settings(APP_ENV="development", ALLOW_TEST_FIXTURES=False)


def test_test_fixtures_are_rejected_outside_test_environment() -> None:
    with pytest.raises(ValidationError, match="only allowed when APP_ENV=test"):
        _settings(
            DATABASE_URL="postgresql://user@127.0.0.1:55432/luxury_development",
            APP_ENV="development",
            ALLOW_TEST_FIXTURES=True,
        )


def test_valid_test_environment_enables_fixtures() -> None:
    settings = _settings()

    assert settings.app_env == "test"
    assert settings.database_name == "luxury_test"
    assert settings.database_is_test is True
    assert settings.fixtures_enabled is True
    assert settings.storage_environment.startswith("test:")


def test_recovery_qa_is_read_only_and_does_not_enable_fixtures() -> None:
    settings = _settings(
        DATABASE_URL="postgresql://user@127.0.0.1:55432/luxury_official_recovery",
        APP_ENV="recovery_qa",
        ALLOW_TEST_FIXTURES=False,
    )

    assert settings.app_env == "recovery_qa"
    assert settings.database_name == "luxury_official_recovery"
    assert settings.fixtures_enabled is False
    assert settings.read_only_runtime is True


def test_recovery_qa_rejects_test_database() -> None:
    with pytest.raises(ValidationError, match="not allowed outside APP_ENV=test"):
        _settings(
            DATABASE_URL="postgresql://user@127.0.0.1:55432/luxury_e2e_test",
            APP_ENV="recovery_qa",
            ALLOW_TEST_FIXTURES=False,
        )
