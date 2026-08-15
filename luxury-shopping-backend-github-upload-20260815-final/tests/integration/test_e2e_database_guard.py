from urllib.parse import urlsplit

import pytest

from app.config import Settings


def _settings(database_url: str, app_env: str, allow_test_fixtures: bool) -> Settings:
    return Settings(
        DATABASE_URL=database_url,
        APP_ENV=app_env,
        ALLOW_TEST_FIXTURES=allow_test_fixtures,
        JWT_SECRET="x" * 64,
    )


def assert_e2e_write_database_is_safe(settings: Settings) -> None:
    database_name = urlsplit(settings.database_url).path.lstrip("/").lower()
    assert settings.app_env == "test"
    assert settings.allow_test_fixtures is True
    assert "test" in database_name
    settings.require_test_fixtures_enabled("unified e2e writes")


def test_e2e_guard_allows_isolated_test_database() -> None:
    settings = _settings(
        "postgresql://luxury_admin@127.0.0.1:5432/luxury_unified_e2e_test",
        "test",
        True,
    )

    assert_e2e_write_database_is_safe(settings)


@pytest.mark.parametrize(
    ("database_url", "app_env", "allow_test_fixtures"),
    [
        ("postgresql://luxury_admin@127.0.0.1:55432/luxury_official_recovery", "recovery_qa", False),
        ("postgresql://luxury_admin@127.0.0.1:5432/luxury_official_recovery", "test", True),
        ("postgresql://luxury_admin@127.0.0.1:5432/luxury_unified_e2e_test", "recovery_qa", True),
        ("postgresql://luxury_admin@127.0.0.1:5432/luxury_unified_e2e_test", "test", False),
    ],
)
def test_e2e_guard_rejects_unsafe_write_targets(
    database_url: str,
    app_env: str,
    allow_test_fixtures: bool,
) -> None:
    with pytest.raises((AssertionError, ValueError, RuntimeError)):
        settings = _settings(database_url, app_env, allow_test_fixtures)
        assert_e2e_write_database_is_safe(settings)
