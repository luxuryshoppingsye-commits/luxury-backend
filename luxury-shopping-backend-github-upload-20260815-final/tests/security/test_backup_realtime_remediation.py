from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import _redact_log_value, app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services.resource_policy import validate_resource_operation
from backend.app.services.secure_backup import (
    BackupCoordinator,
    BackupEncryptionService,
    BackupRestoreVerificationService,
    OffsiteBackupService,
    PGPASSFile,
    PostgreSQLDumpService,
    PostgreSQLTarget,
    ResolvedPostgreSQLTools,
    backup_static_audit,
)
from backend.app.services.realtime import RealtimeTicketService


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing backup/realtime tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing backup/realtime tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if settings.database_name != "luxury_full_cross_platform_e2e_test":
        pytest.fail("Refusing backup/realtime tests outside luxury_full_cross_platform_e2e_test", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing backup/realtime tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing backup/realtime tests on recovery database", pytrace=False)


async def _seed_user(role: str, run_id: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-{role}-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"Backup Realtime {role}"))
        session.add(UserRole(user_id=user.id, role=role))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_pg_command_arguments_do_not_include_url_or_password(tmp_path: Path) -> None:
    _assert_safe_database()
    target = PostgreSQLTarget(
        host="127.0.0.1",
        port=55433,
        username="safe_user",
        password="secret-password-value",
        database="luxury_command_test",
    )
    tools = ResolvedPostgreSQLTools(
        pg_dump=Path("pg_dump"),
        pg_restore=Path("pg_restore"),
        psql=Path("psql"),
        createdb=Path("createdb"),
        dropdb=Path("dropdb"),
        pg_dump_version="pg_dump (PostgreSQL) 18.0",
        pg_restore_version="pg_restore (PostgreSQL) 18.0",
        discovery_source="unit",
    )
    service = PostgreSQLDumpService(target=target, tools=tools, timeout_seconds=5)
    dump_command = service.dump_command(tmp_path / "db.dump")
    restore_command = service.restore_command(tmp_path / "db.dump", "luxury_restore_test")
    joined = " ".join([*dump_command, *restore_command])

    assert "secret-password-value" not in joined
    assert "postgresql://" not in joined
    assert "--no-password" in dump_command
    assert "--host" in dump_command
    assert "--dbname" in dump_command


def test_pgpass_file_is_temporary_and_outside_repository() -> None:
    _assert_safe_database()
    target = PostgreSQLTarget(
        host="127.0.0.1",
        port=55433,
        username="safe_user",
        password="secret-password-value",
        database="luxury_pgpass_test",
    )
    project_root = Path(__file__).resolve().parents[3]
    with PGPASSFile(target) as path:
        assert path.exists()
        assert project_root not in path.resolve().parents
        assert "secret-password-value" in path.read_text(encoding="utf-8")
    assert not path.exists()


@pytest.mark.asyncio
async def test_realtime_ticket_security_and_inventory_authorization() -> None:
    _assert_safe_database()
    run_id = f"brt-{uuid.uuid4().hex[:8]}"
    customer, customer_password = await _seed_user("customer", run_id)
    admin, admin_password = await _seed_user("admin", run_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        customer_headers = await _login(client, customer.email, customer_password)
        admin_headers = await _login(client, admin.email, admin_password)
        denied = await client.post(
            "/api/realtime/tickets",
            headers={**customer_headers, "Origin": "http://127.0.0.1:5190"},
            json={"channels": ["inventory"], "platform": "web"},
        )
        issued = await client.post(
            "/api/realtime/tickets",
            headers={**admin_headers, "Origin": "http://127.0.0.1:5190"},
            json={"channels": ["inventory"], "platform": "web", "deviceId": "browser-a"},
        )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "realtime_channel_denied"
    assert issued.status_code == 201
    ticket_payload = issued.json()["data"]
    assert ticket_payload["websocket_url"] == "/ws/realtime"
    assert "token" not in ticket_payload["websocket_url"]
    assert "inventory" in ticket_payload["channels"]
    raw_ticket = ticket_payload["ticket"]

    sync_model = MODEL_BY_TABLE["sync_events"]
    async with SessionFactory() as session:
        row = (
            await session.execute(
                sync_model.__table__.select()
                .where(sync_model.__table__.c.type == "realtime_ticket")
                .order_by(sync_model.__table__.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
    assert row is not None
    assert row["description"] != raw_ticket
    assert len(row["description"]) == 64
    assert raw_ticket not in str(row["extra_data"])


def test_websocket_rejects_query_string_tokens() -> None:
    _assert_safe_database()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/realtime?token=secret"):
                pass
    assert exc_info.value.code == 4401


@pytest.mark.asyncio
async def test_realtime_event_deduplication_and_generic_resource_block() -> None:
    _assert_safe_database()
    run_id = f"brt-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user("admin", run_id)
    dedupe_key = f"{run_id}:admin-event"
    channel = f"user:{admin.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin.email, admin_password)
        first = await client.post(
            "/api/realtime/events",
            headers=headers,
            json={"channel": channel, "event": "admin.test", "payload": {"scope": "unit"}, "dedupeKey": dedupe_key},
        )
        second = await client.post(
            "/api/realtime/events",
            headers=headers,
            json={"channel": channel, "event": "admin.test", "payload": {"scope": "unit"}, "dedupeKey": dedupe_key},
        )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    sync_model = MODEL_BY_TABLE["sync_events"]
    async with SessionFactory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(sync_model.__table__)
                .where(sync_model.__table__.c.type == f"realtime_event:{channel}")
                .where(sync_model.__table__.c.description == dedupe_key)
            )
        ).scalar_one()
    assert count == 1
    with pytest.raises(Exception):
        validate_resource_operation("backup_records", "select", {"admin"})
    with pytest.raises(Exception):
        validate_resource_operation("backup_records", "insert", {"admin"})


@pytest.mark.asyncio
async def test_backup_create_does_not_mark_ready_without_full_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_safe_database()
    run_id = f"brt-{uuid.uuid4().hex[:8]}"
    admin, _ = await _seed_user("admin", run_id)
    monkeypatch.setenv("BACKUP_STORAGE_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("BACKUP_OFFSITE_DIR", str(tmp_path / "offsite"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(tmp_path / "secret" / "backup.key"))
    get_settings.cache_clear()
    try:
        async with SessionFactory() as session:
            result = await BackupCoordinator().create_backup(session, actor=admin, selected_tables=["orders"])
            await session.commit()
    finally:
        get_settings.cache_clear()
    assert result["status"] in {"ready", "failed"}
    if result["status"] == "ready":
        assert result["encrypted"] is True
        assert result["offsite_status"] == "verified"
        assert result["ready_has_verified_restore"] is True
    else:
        assert result["download_url"] is None
        assert result["ready_has_verified_restore"] is False


def test_static_backup_audit_has_no_hardcoded_pg18_or_url_pg_tool_invocation() -> None:
    _assert_safe_database()
    audit = backup_static_audit()
    assert audit["hardcoded_postgresql_18_paths"] == 0
    assert not audit["findings"], audit


def test_backup_encryption_rejects_tampered_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_safe_database()
    key_file = tmp_path / "secret" / "backup.key"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY_FILE", str(key_file))
    get_settings.cache_clear()
    try:
        source = tmp_path / "bundle.tar.gz"
        encrypted = tmp_path / "bundle.tar.gz.fernet"
        decrypted = tmp_path / "decrypted.tar.gz"
        source.write_bytes(b"backup-bundle")
        service = BackupEncryptionService(key_file)
        result = service.encrypt_file(source, encrypted)
        assert result["encrypted_size"] > 0
        service.decrypt_to(encrypted, decrypted)
        assert decrypted.read_bytes() == b"backup-bundle"
        encrypted.write_bytes(encrypted.read_bytes()[:-8] + b"tampered")
        with pytest.raises(RuntimeError, match="backup_decryption_failed"):
            service.decrypt_to(encrypted, tmp_path / "bad.tar.gz")
    finally:
        get_settings.cache_clear()


def test_restored_bundle_integrity_rejects_modified_checksum(tmp_path: Path) -> None:
    _assert_safe_database()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "database.dump").write_bytes(b"PGDMP\ncontent")
    (bundle / "database-metadata.json").write_text(
        '{"dump_file":"database.dump"}',
        encoding="utf-8",
    )
    (bundle / "file-manifest.json").write_text(
        '{"file_count":0,"files":[]}',
        encoding="utf-8",
    )
    (bundle / "manifest.json").write_text(
        '{"format_version":"luxury-secure-backup-v1"}',
        encoding="utf-8",
    )
    (bundle / "checksums.sha256").write_text(
        "0" * 64 + "  database.dump\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="restored_checksum_mismatch"):
        BackupRestoreVerificationService._verify_extracted_bundle(bundle)


@pytest.mark.asyncio
async def test_backup_download_requires_verified_encrypted_offsite_bundle(tmp_path: Path) -> None:
    _assert_safe_database()
    run_id = f"brt-{uuid.uuid4().hex[:8]}"
    admin, admin_password = await _seed_user("admin", run_id)
    model = MODEL_BY_TABLE["backup_records"]
    async with SessionFactory() as session:
        row = model(
            user_id=admin.id,
            status="ready",
            path="",
            description="tampered ready backup",
            extra_data={"encrypted_bundle_key": "missing.tar.gz.fernet"},
        )
        session.add(row)
        await session.commit()
        backup_id = str(row.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin.email, admin_password)
        response = await client.get(f"/backups/{backup_id}/download", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "backup_not_verified"


@pytest.mark.asyncio
async def test_realtime_ticket_is_bound_to_issued_origin() -> None:
    _assert_safe_database()
    run_id = f"brt-{uuid.uuid4().hex[:8]}"
    admin, _ = await _seed_user("admin", run_id)
    async with SessionFactory() as session:
        ticket = await RealtimeTicketService().issue(
            session,
            user=admin,
            roles={"admin"},
            requested_channels=["notifications"],
            device_id="browser-a",
            platform="web",
            origin="http://127.0.0.1:5190",
        )
        await session.commit()

    async with SessionFactory() as session:
        with pytest.raises(Exception) as exc_info:
            await RealtimeTicketService().consume(
                session,
                ticket=ticket.token,
                origin="http://localhost:5190",
            )
    assert getattr(exc_info.value, "detail", None) == "realtime_ticket_origin_mismatch"


def test_filesystem_offsite_provider_is_not_allowed_for_production(tmp_path: Path) -> None:
    _assert_safe_database()

    class _Settings:
        app_env = "production"

    encrypted = tmp_path / "backup.tar.gz.fernet"
    encrypted.write_bytes(b"encrypted")
    service = OffsiteBackupService(tmp_path / "offsite", provider="filesystem", settings=_Settings())

    with pytest.raises(RuntimeError, match="filesystem_offsite_not_allowed"):
        service.upload_and_verify(encrypted)


def test_log_redaction_removes_realtime_and_database_secrets() -> None:
    _assert_safe_database()
    raw = (
        'GET /ws/realtime?token=secret&x=1 '
        'Authorization: Bearer jwt-secret '
        'Sec-WebSocket-Protocol: luxury.realtime.v1, rt.ticket-secret '
        'postgresql+asyncpg://user:password@127.0.0.1:55433/db'
    )
    redacted = str(_redact_log_value(raw))
    assert "secret" not in redacted
    assert "password" not in redacted
    assert "token=***" in redacted
    assert "rt.***" in redacted
    assert "postgresql+asyncpg://***@127.0.0.1:55433/db" in redacted
