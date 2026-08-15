from __future__ import annotations

from pathlib import Path

from backend.app.api.routes import operations
from backend.app.config import Settings, get_settings


def test_storage_diagnostics_reports_seed_and_product_files(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    sample = upload_dir / operations.SEED_UPLOADS_SAMPLE
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF1234WEBPdata")
    seed_zip = tmp_path / "uploads_seed.zip"
    seed_zip.write_bytes(b"seed")

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unit:user@localhost/luxury_test")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_TEST_FIXTURES", "true")
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-value-with-32-characters-minimum")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(operations, "SEED_UPLOADS_ZIP", seed_zip)
    get_settings.cache_clear()
    try:
        diagnostics = operations._storage_diagnostics()
    finally:
        get_settings.cache_clear()

    assert diagnostics["upload_dir_exists"] is True
    assert diagnostics["upload_dir_writable"] is True
    assert diagnostics["product_upload_files"] == 1
    assert diagnostics["seed_uploads_zip_exists"] is True
    assert diagnostics["seed_uploads_zip_bytes"] == 4
    assert diagnostics["seed_sample_exists"] is True


def test_upload_dir_falls_back_when_configured_path_is_unwritable(monkeypatch, tmp_path):
    blocked_file = tmp_path / "not-a-directory"
    blocked_file.write_text("blocked", encoding="utf-8")
    fallback_dir = tmp_path / "fallback-uploads"

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unit:user@localhost/luxury_test")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_TEST_FIXTURES", "true")
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-value-with-32-characters-minimum")
    monkeypatch.setenv("UPLOAD_DIR", str(blocked_file / "uploads"))
    monkeypatch.setenv("UPLOAD_FALLBACK_DIR", str(fallback_dir))

    settings = Settings()

    assert settings.resolved_upload_dir == fallback_dir.resolve()
    assert settings.resolved_upload_dir.is_dir()


def test_production_upload_dir_uses_runtime_fallback_when_disk_missing(monkeypatch, tmp_path):
    blocked_file = tmp_path / "not-a-directory"
    blocked_file.write_text("blocked", encoding="utf-8")
    runtime_tmp = tmp_path / "runtime-tmp"

    monkeypatch.setattr("backend.app.config.tempfile.gettempdir", lambda: str(runtime_tmp))
    settings = Settings(
        DATABASE_URL="postgresql://unit@db.example.com:5432/luxury_operational",
        APP_ENV="production",
        ALLOW_TEST_FIXTURES=False,
        JWT_SECRET="unit-test-secret-value-with-32-characters-minimum",
        CORS_ORIGINS="https://luxuryshoppings.com,https://www.luxuryshoppings.com",
        REALTIME_ALLOWED_ORIGINS="https://luxuryshoppings.com,https://www.luxuryshoppings.com",
        FRONTEND_PUBLIC_URL="https://luxuryshoppings.com",
        RENDER_PUBLIC_URL="https://luxury-backend-xy9d.onrender.com",
        API_BASE_URL="https://luxury-backend-xy9d.onrender.com",
        APP_PUBLIC_URL="https://luxury-backend-xy9d.onrender.com",
        WS_BASE_URL="wss://luxury-backend-xy9d.onrender.com",
        STORAGE_PROVIDER="r2",
        R2_ENDPOINT_URL="https://account.r2.cloudflarestorage.com",
        R2_BUCKET="luxury-images-prod",
        R2_ACCESS_KEY_ID="unit-access-key",
        R2_SECRET_ACCESS_KEY="unit-secret-key",
        R2_PUBLIC_BASE_URL="https://images.luxuryshoppings.com",
        UPLOAD_DIR=Path(blocked_file / "uploads"),
    )

    assert settings.resolved_upload_dir == (runtime_tmp / "luxury-backend-uploads").resolve()
    assert settings.resolved_upload_dir.is_dir()
