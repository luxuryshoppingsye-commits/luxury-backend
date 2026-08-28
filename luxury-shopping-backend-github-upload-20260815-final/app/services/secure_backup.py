from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ..config import get_settings
from ..database import engine
from ..models import MODEL_BY_TABLE
from ..models.domain import FileAsset, User
from ..repositories.resources import serialize_record
from ..storage.files import FileStorage


BACKUP_LOCK_KEY = 0x4C555855525942  # LUXURYB
BACKUP_STATUSES = frozenset(
    {
        "requested",
        "acquiring_lock",
        "dumping_database",
        "collecting_files",
        "building_manifest",
        "encrypting",
        "verifying_local_bundle",
        "uploading_offsite",
        "verifying_offsite_copy",
        "restoring_test_database",
        "verifying_restored_files",
        "ready",
        "failed",
        "cancelled",
        "expired",
        "deleting",
    }
)


@dataclass(frozen=True)
class PostgreSQLTarget:
    host: str
    port: int
    username: str
    password: str
    database: str

    @classmethod
    def from_url(cls, value: str) -> "PostgreSQLTarget":
        parsed = urlsplit(value.replace("postgresql+asyncpg://", "postgresql://", 1))
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise RuntimeError("DATABASE_URL must be PostgreSQL")
        database = parsed.path.lstrip("/")
        if not database:
            raise RuntimeError("DATABASE_URL database name is required")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=int(parsed.port or 5432),
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=database,
        )


@dataclass(frozen=True)
class ResolvedPostgreSQLTools:
    pg_dump: Path
    pg_restore: Path
    psql: Path | None
    createdb: Path | None
    dropdb: Path | None
    pg_dump_version: str
    pg_restore_version: str
    discovery_source: str


class PostgreSQLToolResolver:
    def __init__(self, *, configured_bin_dir: Path | None = None) -> None:
        self.configured_bin_dir = configured_bin_dir

    def resolve(self) -> ResolvedPostgreSQLTools:
        candidates = self._candidate_dirs()
        pg_dump = self._resolve_one("pg_dump", candidates)
        pg_restore = self._resolve_one("pg_restore", candidates)
        psql = self._resolve_optional("psql", candidates)
        createdb = self._resolve_optional("createdb", candidates)
        dropdb = self._resolve_optional("dropdb", candidates)
        return ResolvedPostgreSQLTools(
            pg_dump=pg_dump,
            pg_restore=pg_restore,
            psql=psql,
            createdb=createdb,
            dropdb=dropdb,
            pg_dump_version=self._version(pg_dump),
            pg_restore_version=self._version(pg_restore),
            discovery_source=str(pg_dump.parent),
        )

    def _candidate_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        if self.configured_bin_dir is not None:
            dirs.append(self.configured_bin_dir)
        for name in ("pg_dump", "pg_restore", "psql", "createdb", "dropdb"):
            found = shutil.which(name)
            if found:
                dirs.append(Path(found).resolve().parent)
        bindir = shutil.which("pg_config")
        if bindir:
            try:
                result = subprocess.run(
                    [bindir, "--bindir"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    shell=False,
                )
                if result.stdout.strip():
                    dirs.append(Path(result.stdout.strip()).resolve())
            except (OSError, subprocess.SubprocessError):
                pass
        if os.name == "nt":
            roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")), Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
            for root in roots:
                pg_root = root / "PostgreSQL"
                if not pg_root.is_dir():
                    continue
                for child in sorted(pg_root.iterdir(), reverse=True):
                    if child.is_dir():
                        dirs.append(child / "bin")
                        runtime = child / "pgAdmin 4" / "runtime"
                        if runtime.is_dir():
                            dirs.append(runtime)
        seen: set[Path] = set()
        unique: list[Path] = []
        for item in dirs:
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    @staticmethod
    def _executable_name(name: str) -> str:
        return f"{name}.exe" if os.name == "nt" else name

    def _resolve_one(self, name: str, dirs: list[Path]) -> Path:
        resolved = self._resolve_optional(name, dirs)
        if resolved is None:
            raise RuntimeError(f"{name}_not_found")
        return resolved

    def _resolve_optional(self, name: str, dirs: list[Path]) -> Path | None:
        exe = self._executable_name(name)
        for directory in dirs:
            target = directory / exe
            if target.is_file():
                return target.resolve()
        return None

    @staticmethod
    def _version(path: Path) -> str:
        result = subprocess.run(
            [str(path), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        return result.stdout.strip()


class PGPASSFile:
    def __init__(self, target: PostgreSQLTarget) -> None:
        self.target = target
        root = Path(tempfile.gettempdir()) / "luxury-runtime-secrets"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"pgpass-{uuid.uuid4().hex}.conf"

    def __enter__(self) -> Path:
        content = f"{self.target.host}:{self.target.port}:*:{self.target.username}:{self.target.password}\n"
        self.path.write_text(content, encoding="utf-8")
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(["icacls", str(self.path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True, shell=False)
        return self.path

    def __exit__(self, *_: object) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _redact(value: str) -> str:
    return re.sub(r"(postgres(?:ql)?(?:\+asyncpg)?://)[^@\s]+@", r"\1***@", value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_bundle_path(root: Path, relative_path: str) -> Path:
    relative = str(relative_path or "").replace("\\", "/").lstrip("/")
    parsed = PurePosixPath(relative)
    if not relative or parsed.is_absolute() or ".." in parsed.parts:
        raise RuntimeError("unsafe_backup_archive_path")
    target = (root / Path(*parsed.parts)).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError("unsafe_backup_archive_path")
    return target


class PostgreSQLDumpService:
    def __init__(self, *, target: PostgreSQLTarget, tools: ResolvedPostgreSQLTools, timeout_seconds: int) -> None:
        self.target = target
        self.tools = tools
        self.timeout_seconds = timeout_seconds

    def dump_command(self, destination: Path) -> list[str]:
        return [
            str(self.tools.pg_dump),
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-acl",
            "--no-password",
            "--host",
            self.target.host,
            "--port",
            str(self.target.port),
            "--username",
            self.target.username,
            "--dbname",
            self.target.database,
            "--file",
            str(destination),
        ]

    def restore_command(self, source: Path, database: str) -> list[str]:
        return [
            str(self.tools.pg_restore),
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--single-transaction",
            "--no-password",
            "--host",
            self.target.host,
            "--port",
            str(self.target.port),
            "--username",
            self.target.username,
            "--dbname",
            database,
            str(source),
        ]

    def run_dump(self, destination: Path) -> dict[str, Any]:
        with PGPASSFile(self.target) as pgpass:
            env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
            env["PGPASSFILE"] = str(pgpass)
            command = self.dump_command(destination)
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                shell=False,
            )
        stderr = _redact(result.stderr or "")
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump_failed:{stderr[-500:]}")
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise RuntimeError("pg_dump_empty_file")
        list_result = subprocess.run(
            [str(self.tools.pg_restore), "--list", str(destination)],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
        if list_result.returncode != 0:
            raise RuntimeError(f"pg_restore_list_failed:{_redact(list_result.stderr or '')[-500:]}")
        return {
            "command_argument_secret_count": sum(1 for arg in command if self.target.password and self.target.password in arg),
            "command_argument_database_url_count": sum(1 for arg in command if "://" in arg and self.target.database in arg),
            "shell": False,
            "dump_size": destination.stat().st_size,
            "dump_sha256": _sha256(destination),
            "restore_list_entries": len([line for line in list_result.stdout.splitlines() if line.strip()]),
        }


class FileSnapshotService:
    def __init__(self, *, upload_root: Path, storage: FileStorage | None = None) -> None:
        self.upload_root = upload_root.resolve()
        self.storage = storage

    @staticmethod
    def _safe_archive_target(destination: Path, storage_key: str) -> Path:
        target = (destination / "files" / storage_key).resolve()
        destination_root = destination.resolve()
        try:
            target.relative_to(destination_root)
        except ValueError as exc:
            raise RuntimeError("unsafe_file_asset_path") from exc
        return target

    def _download_r2_file(self, *, storage_key: str, target: Path) -> None:
        if self.storage is None:
            raise RuntimeError("r2_storage_unavailable")
        body = None
        try:
            response = self.storage._r2_client().get_object(
                Bucket=str(self.storage.settings.r2_bucket),
                Key=storage_key,
            )
            body = response["Body"]
            with target.open("wb") as handle:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        except Exception as exc:
            raise RuntimeError("file_missing_from_r2") from exc
        finally:
            if body is not None:
                body.close()

    async def collect(self, session: AsyncSession, destination: Path) -> dict[str, Any]:
        rows = (
            await session.execute(
                text(
                    """
                    select id::text, policy_key, visibility, storage_provider, storage_bucket, storage_key,
                           original_filename, content_type, size_bytes, checksum_sha256,
                           entity_type, entity_id::text, created_at, scan_status
                    from file_assets
                    where deleted_at is null and status = 'available' and coalesce(storage_key, '') <> ''
                    order by created_at asc
                    """
                )
            )
        ).mappings().all()
        destination.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        for row in rows:
            storage_key = str(row["storage_key"]).replace("\\", "/").lstrip("/")
            archive_path = f"files/{storage_key}"
            target = self._safe_archive_target(destination, storage_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            provider = str(row.get("storage_provider") or "local_uploads")
            source = (self.upload_root / storage_key).resolve()
            if source != self.upload_root and self.upload_root not in source.parents:
                raise RuntimeError("unsafe_file_asset_path")
            if provider == "cloudflare_r2":
                try:
                    self._download_r2_file(storage_key=storage_key, target=target)
                except Exception as exc:
                    raise RuntimeError(f"file_missing:{row['id']}") from exc
            elif source.is_file():
                shutil.copy2(source, target)
            elif self.storage is not None and str(getattr(self.storage.settings, "storage_provider", "")).strip().lower() == "r2":
                # Render's local filesystem is ephemeral. Older file_assets
                # rows may still say local_uploads even though the object was
                # migrated to R2, so recover those rows from the durable
                # object store before declaring the backup incomplete.
                try:
                    self._download_r2_file(storage_key=storage_key, target=target)
                except Exception as exc:
                    raise RuntimeError(f"file_missing:{row['id']}") from exc
            else:
                raise RuntimeError(f"file_missing:{row['id']}")
            if not target.is_file():
                raise RuntimeError(f"file_missing:{row['id']}")
            expected_size = row.get("size_bytes")
            if expected_size is not None and target.stat().st_size != int(expected_size):
                raise RuntimeError(f"file_size_mismatch:{row['id']}")
            actual_sha = _sha256(target)
            if row["checksum_sha256"] and actual_sha != row["checksum_sha256"]:
                raise RuntimeError(f"file_checksum_mismatch:{row['id']}")
            files.append(
                {
                    "file_id": row["id"],
                    "storage_namespace": row["policy_key"],
                    "relative_archive_path": archive_path,
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "size": target.stat().st_size,
                    "checksum": actual_sha,
                    "mime": row["content_type"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "visibility": row["visibility"],
                    "scan_status": row["scan_status"],
                }
            )
        return {"file_count": len(files), "files": files}


class BackupEncryptionService:
    def __init__(self, key_file: Path) -> None:
        self.key_file = key_file.resolve()

    def _key(self) -> bytes:
        settings = get_settings()
        if not self.key_file.exists():
            configured_key = str(settings.backup_encryption_key or "").strip()
            if configured_key:
                # Accept a normal Fernet key, while also supporting a secret
                # value generated by Render.  The latter is deterministically
                # converted into a Fernet key and remains stable across restarts.
                candidate = configured_key.encode("utf-8")
                try:
                    Fernet(candidate)
                except (ValueError, TypeError):
                    candidate = base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                self.key_file.write_bytes(candidate)
            elif settings.app_env != "test":
                raise RuntimeError("backup_encryption_key_missing")
            else:
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                self.key_file.write_bytes(Fernet.generate_key())
            try:
                self.key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        return self.key_file.read_bytes().strip()

    @property
    def key_id(self) -> str:
        return hashlib.sha256(self._key()).hexdigest()[:16]

    def encrypt_file(self, source: Path, destination: Path) -> dict[str, Any]:
        fernet = Fernet(self._key())
        destination.parent.mkdir(parents=True, exist_ok=True)
        encrypted = fernet.encrypt(source.read_bytes())
        destination.write_bytes(encrypted)
        return {"encrypted_size": destination.stat().st_size, "encrypted_sha256": _sha256(destination), "key_id": self.key_id}

    def decrypt_to(self, source: Path, destination: Path) -> None:
        fernet = Fernet(self._key())
        try:
            plaintext = fernet.decrypt(source.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError("backup_decryption_failed") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(plaintext)


def resolve_backup_offsite_provider(settings: Any) -> str:
    """Choose durable offsite storage for an operational deployment.

    The Render blueprint historically set ``filesystem`` while configuring
    Cloudflare R2 for uploads. A filesystem copy is not durable on Render;
    when the existing R2 credentials are complete, use the S3-compatible R2
    endpoint automatically so older environment configurations remain safe.
    """
    provider = str(getattr(settings, "backup_offsite_provider", "filesystem") or "filesystem").strip().lower()
    if provider != "filesystem" or str(getattr(settings, "app_env", "")).strip().lower() not in {"staging", "production"}:
        return provider
    r2_values = (
        getattr(settings, "r2_endpoint_url", ""),
        getattr(settings, "r2_bucket", ""),
        getattr(settings, "r2_access_key_id", ""),
        getattr(settings, "r2_secret_access_key", ""),
    )
    return "s3" if all(str(value or "").strip() for value in r2_values) else provider


class OffsiteBackupService:
    def __init__(self, offsite_dir: Path, *, provider: str = "filesystem", settings: Any | None = None) -> None:
        self.offsite_dir = offsite_dir.resolve()
        self.provider = provider
        self.settings = settings or get_settings()

    def upload_and_verify(self, encrypted_bundle: Path) -> dict[str, Any]:
        if self.provider == "disabled":
            raise RuntimeError("backup_offsite_provider_disabled")
        if self.provider == "s3":
            return self._upload_and_verify_s3(encrypted_bundle)
        if self.settings.app_env in {"staging", "production"}:
            raise RuntimeError("filesystem_offsite_not_allowed_for_operational_environment")
        self.offsite_dir.mkdir(parents=True, exist_ok=True)
        target = self.offsite_dir / encrypted_bundle.name
        shutil.copy2(encrypted_bundle, target)
        local_sha = _sha256(encrypted_bundle)
        offsite_sha = _sha256(target)
        if local_sha != offsite_sha:
            raise RuntimeError("offsite_checksum_mismatch")
        return {
            "offsite_status": "verified",
            "offsite_key": target.name,
            "offsite_sha256": offsite_sha,
            "offsite_size": target.stat().st_size,
        }

    def _s3_setting(self, backup_name: str, r2_name: str, default: str = "") -> str:
        value = getattr(self.settings, backup_name, None)
        if value in (None, ""):
            value = getattr(self.settings, r2_name, default)
        return str(value or default).strip()

    def _upload_and_verify_s3(self, encrypted_bundle: Path) -> dict[str, Any]:
        bucket = self._s3_setting("backup_s3_bucket", "r2_bucket")
        if not bucket:
            raise RuntimeError("backup_s3_bucket_missing")
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("backup_s3_client_missing") from exc

        prefix = self._s3_setting("backup_s3_prefix", "", "luxury-secure-backups").strip("/")
        object_key = f"{prefix}/{encrypted_bundle.name}" if prefix else encrypted_bundle.name
        local_sha = _sha256(encrypted_bundle)
        kwargs: dict[str, Any] = {}
        endpoint = self._s3_setting("backup_s3_endpoint_url", "r2_endpoint_url")
        region = self._s3_setting("backup_s3_region", "r2_region", "auto")
        access_key = self._s3_setting("backup_s3_access_key_id", "r2_access_key_id")
        secret_key = self._s3_setting("backup_s3_secret_access_key", "r2_secret_access_key")
        session_token = self._s3_setting("backup_s3_session_token", "")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        if access_key:
            kwargs["aws_access_key_id"] = access_key
        if secret_key:
            kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
        client = boto3.client("s3", **kwargs)
        metadata = {"sha256": local_sha, "encrypted": "true", "format": "luxury-secure-backup-v1"}
        client.upload_file(
            str(encrypted_bundle),
            bucket,
            object_key,
            ExtraArgs={
                "Metadata": metadata,
                "ContentType": "application/octet-stream",
            },
        )
        head = client.head_object(Bucket=bucket, Key=object_key)
        remote_size = int(head.get("ContentLength") or 0)
        remote_sha = str((head.get("Metadata") or {}).get("sha256") or "")
        if remote_size != encrypted_bundle.stat().st_size or remote_sha != local_sha:
            raise RuntimeError("offsite_s3_verification_failed")
        return {
            "offsite_provider": "s3",
            "offsite_status": "verified",
            "offsite_key": object_key,
            "offsite_sha256": local_sha,
            "offsite_size": remote_size,
        }

    def download_and_verify(
        self,
        offsite_key: str,
        destination: Path,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Restore a verified offsite object to a short-lived local file."""
        key = str(offsite_key or "").strip()
        if not key:
            raise RuntimeError("backup_offsite_key_missing")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.provider == "s3":
            bucket = self._s3_setting("backup_s3_bucket", "r2_bucket")
            if not bucket:
                raise RuntimeError("backup_s3_bucket_missing")
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("backup_s3_client_missing") from exc
            kwargs: dict[str, Any] = {}
            endpoint = self._s3_setting("backup_s3_endpoint_url", "r2_endpoint_url")
            region = self._s3_setting("backup_s3_region", "r2_region", "auto")
            access_key = self._s3_setting("backup_s3_access_key_id", "r2_access_key_id")
            secret_key = self._s3_setting("backup_s3_secret_access_key", "r2_secret_access_key")
            session_token = self._s3_setting("backup_s3_session_token", "")
            if endpoint:
                kwargs["endpoint_url"] = endpoint
            if region:
                kwargs["region_name"] = region
            if access_key:
                kwargs["aws_access_key_id"] = access_key
            if secret_key:
                kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                kwargs["aws_session_token"] = session_token
            boto3.client("s3", **kwargs).download_file(bucket, key, str(destination))
        elif self.provider == "filesystem":
            source = (self.offsite_dir / key).resolve()
            if self.offsite_dir not in source.parents:
                raise RuntimeError("backup_offsite_path_invalid")
            if not source.is_file():
                raise RuntimeError("backup_offsite_file_not_found")
            shutil.copy2(source, destination)
        else:
            raise RuntimeError("backup_offsite_provider_disabled")

        actual_size = destination.stat().st_size if destination.is_file() else 0
        actual_sha256 = _sha256(destination) if actual_size else ""
        if actual_size != int(expected_size) or actual_sha256 != str(expected_sha256):
            destination.unlink(missing_ok=True)
            raise RuntimeError("backup_offsite_download_verification_failed")


class BackupRestoreVerificationService:
    def __init__(self, *, target: PostgreSQLTarget, tools: ResolvedPostgreSQLTools, timeout_seconds: int) -> None:
        self.target = target
        self.tools = tools
        self.timeout_seconds = timeout_seconds

    async def verify(self, encrypted_bundle: Path, encryption: BackupEncryptionService) -> dict[str, Any]:
        async with engine.connect() as conn:
            can_create = bool(
                (
                    await conn.execute(
                        text("select rolcreatedb from pg_roles where rolname=current_user")
                    )
                ).scalar()
            )
        if not can_create:
            raise RuntimeError("restore_blocked_insufficient_createdb_privilege")
        if self.tools.createdb is None or self.tools.dropdb is None or self.tools.psql is None:
            raise RuntimeError("restore_blocked_postgresql_restore_tools_missing")
        restore_db = f"{self.target.database}_restore_verify_{uuid.uuid4().hex[:8]}"
        staging = Path(tempfile.mkdtemp(prefix="luxury-restore-"))
        decrypted = staging / "bundle.tar.gz"
        try:
            encryption.decrypt_to(encrypted_bundle, decrypted)
            bundle_dir = staging / "bundle"
            with tarfile.open(decrypted, "r:gz") as archive:
                for member in archive.getmembers():
                    _safe_bundle_path(bundle_dir, member.name)
                archive.extractall(bundle_dir, filter="data")
            integrity = self._verify_extracted_bundle(bundle_dir)
            dump = bundle_dir / "database.dump"
            if not dump.is_file() or dump.stat().st_size <= 0:
                raise RuntimeError("restored_bundle_missing_database_dump")
            with PGPASSFile(self.target) as pgpass:
                env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
                env["PGPASSFILE"] = str(pgpass)
                create_result = subprocess.run(
                    [
                        str(self.tools.createdb or self.tools.psql),
                        "--host",
                        self.target.host,
                        "--port",
                        str(self.target.port),
                        "--username",
                        self.target.username,
                        "--no-password",
                        restore_db,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    env=env,
                    shell=False,
                )
                if create_result.returncode != 0:
                    raise RuntimeError(f"createdb_failed:{_redact(create_result.stderr or '')[-500:]}")
                restore_result = subprocess.run(
                    PostgreSQLDumpService(target=self.target, tools=self.tools, timeout_seconds=self.timeout_seconds).restore_command(dump, restore_db),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    env=env,
                    shell=False,
                )
                if restore_result.returncode != 0:
                    raise RuntimeError(f"pg_restore_failed:{_redact(restore_result.stderr or '')[-500:]}")
                if self.tools.psql is not None:
                    smoke = subprocess.run(
                        [
                            str(self.tools.psql),
                            "--host",
                            self.target.host,
                            "--port",
                            str(self.target.port),
                            "--username",
                            self.target.username,
                            "--dbname",
                            restore_db,
                            "--no-password",
                            "--tuples-only",
                            "--command",
                            "select count(*) from information_schema.tables where table_schema='public'",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout_seconds,
                        env=env,
                        shell=False,
                    )
                    if smoke.returncode != 0:
                        raise RuntimeError(f"restore_smoke_failed:{_redact(smoke.stderr or '')[-500:]}")
            return {
                "restore_verification_status": "verified",
                "restore_database": restore_db,
                "smoke_test": "passed",
                **integrity,
            }
        finally:
            with PGPASSFile(self.target) as pgpass:
                env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
                env["PGPASSFILE"] = str(pgpass)
                if self.tools.dropdb is not None:
                    subprocess.run(
                        [
                            str(self.tools.dropdb),
                            "--if-exists",
                            "--host",
                            self.target.host,
                            "--port",
                            str(self.target.port),
                            "--username",
                            self.target.username,
                            "--no-password",
                            restore_db,
                        ],
                        capture_output=True,
                        timeout=self.timeout_seconds,
                        env=env,
                        shell=False,
                    )
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _verify_extracted_bundle(bundle_dir: Path) -> dict[str, Any]:
        manifest_path = bundle_dir / "manifest.json"
        database_metadata_path = bundle_dir / "database-metadata.json"
        file_manifest_path = bundle_dir / "file-manifest.json"
        checksums_path = bundle_dir / "checksums.sha256"
        for required in (manifest_path, database_metadata_path, file_manifest_path, checksums_path):
            if not required.is_file():
                raise RuntimeError(f"restored_bundle_missing:{required.name}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != "luxury-secure-backup-v1":
            raise RuntimeError("restored_bundle_format_mismatch")
        db_meta = json.loads(database_metadata_path.read_text(encoding="utf-8"))
        if db_meta.get("dump_file") != "database.dump":
            raise RuntimeError("restored_database_metadata_invalid")

        checksum_count = 0
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                raise RuntimeError("restored_checksum_file_invalid")
            expected, relative = parts[0], parts[1].strip()
            target = _safe_bundle_path(bundle_dir, relative)
            if not target.is_file():
                raise RuntimeError(f"restored_checksum_target_missing:{relative}")
            if _sha256(target) != expected:
                raise RuntimeError(f"restored_checksum_mismatch:{relative}")
            checksum_count += 1

        file_manifest = json.loads(file_manifest_path.read_text(encoding="utf-8"))
        files = file_manifest.get("files") if isinstance(file_manifest, dict) else None
        if not isinstance(files, list):
            raise RuntimeError("restored_file_manifest_invalid")
        for item in files:
            if not isinstance(item, dict):
                raise RuntimeError("restored_file_manifest_invalid")
            relative = str(item.get("relative_archive_path") or "")
            target = _safe_bundle_path(bundle_dir, relative)
            if not target.is_file():
                raise RuntimeError(f"restored_file_missing:{item.get('file_id')}")
            if int(item.get("size") or 0) != target.stat().st_size:
                raise RuntimeError(f"restored_file_size_mismatch:{item.get('file_id')}")
            checksum = str(item.get("checksum") or "")
            if checksum and _sha256(target) != checksum:
                raise RuntimeError(f"restored_file_checksum_mismatch:{item.get('file_id')}")

        return {
            "manifest_verified": True,
            "checksum_file_count": checksum_count,
            "restored_file_count": len(files),
        }


class BackupRetentionService:
    def __init__(self, *, storage_dir: Path, offsite_dir: Path, retention_days: int) -> None:
        self.storage_dir = storage_dir.resolve()
        self.offsite_dir = offsite_dir.resolve()
        self.retention_days = retention_days

    async def apply(self, session: AsyncSession) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        model = MODEL_BY_TABLE["backup_records"]
        rows = (
            await session.execute(
                text(
                    """
                    select id::text, path, extra_data
                    from backup_records
                    where deleted_at is null
                      and created_at < :cutoff
                      and coalesce(extra_data->>'legal_hold', 'false') <> 'true'
                      and id not in (
                        select id from backup_records
                        where deleted_at is null and status = 'ready'
                        order by created_at desc
                        limit 1
                      )
                    """
                ),
                {"cutoff": cutoff},
            )
        ).mappings().all()
        deleted_files = 0
        for row in rows:
            for value in (row["path"], (row["extra_data"] or {}).get("offsite_key")):
                if not value:
                    continue
                target = Path(str(value))
                if not target.is_absolute():
                    candidates = [self.storage_dir / str(value), self.offsite_dir / str(value)]
                else:
                    candidates = [target]
                for candidate in candidates:
                    resolved = candidate.resolve()
                    if resolved.is_file() and (self.storage_dir in resolved.parents or self.offsite_dir in resolved.parents):
                        resolved.unlink()
                        deleted_files += 1
            record = await session.get(model, uuid.UUID(row["id"]))
            if record is not None:
                record.status = "expired"
                record.deleted_at = datetime.now(timezone.utc)
        await session.flush()
        return {"expired_records": len(rows), "deleted_files": deleted_files}


class BackupCoordinator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.target = PostgreSQLTarget.from_url(self.settings.database_url)
        self.tools = PostgreSQLToolResolver(configured_bin_dir=self.settings.backup_pg_bin_dir).resolve()
        self.encryption = BackupEncryptionService(self.settings.resolved_backup_encryption_key_file)
        offsite_provider = resolve_backup_offsite_provider(self.settings)
        self.offsite = OffsiteBackupService(
            self.settings.resolved_backup_offsite_dir,
            provider=offsite_provider,
            settings=self.settings,
        )
        self.retention = BackupRetentionService(
            storage_dir=self.settings.resolved_backup_storage_dir,
            offsite_dir=self.settings.resolved_backup_offsite_dir,
            retention_days=self.settings.backup_retention_days,
        )

    async def _set_status(self, session: AsyncSession, row: Any, status: str, **extra: Any) -> None:
        if status not in BACKUP_STATUSES:
            raise RuntimeError("invalid_backup_status")
        row.status = status
        payload = dict(row.extra_data or {})
        payload.update({key: value for key, value in extra.items() if value is not None})
        payload["status_history"] = [
            *payload.get("status_history", []),
            {"status": status, "at": datetime.now(timezone.utc).isoformat()},
        ]
        row.extra_data = payload
        await session.flush()

    async def create_backup(self, session: AsyncSession, *, actor: User, selected_tables: list[str] | None = None) -> dict[str, Any]:
        model = MODEL_BY_TABLE["backup_records"]
        row = model(
            user_id=actor.id,
            status="requested",
            description="encrypted full backup bundle",
            path="",
            extra_data={
                "format_version": "luxury-secure-backup-v1",
                "selected_tables": sorted(set(selected_tables or [])),
                "created_by": str(actor.id),
                "policy": {
                    "encrypted_bundle_required": True,
                    "offsite_required": True,
                    "restore_verification_required": self.settings.backup_require_restore_verification,
                },
            },
        )
        session.add(row)
        await session.flush()
        await self._set_status(session, row, "acquiring_lock")

        lock_conn: AsyncConnection | None = None
        acquired = False
        backup_id = row.id
        workspace = Path(tempfile.mkdtemp(prefix=f"luxury-backup-{backup_id}-"))
        plaintext_bundle = workspace / "bundle.tar.gz"
        dump_path = workspace / "bundle" / "database.dump"
        files_dir = workspace / "bundle"
        encrypted_name = f"backup_{backup_id}.tar.gz.fernet"
        encrypted_path = self.settings.resolved_backup_storage_dir / encrypted_name
        try:
            lock_conn = await engine.connect()
            acquired = bool((await lock_conn.execute(text("select pg_try_advisory_lock(:key)"), {"key": BACKUP_LOCK_KEY})).scalar())
            if not acquired:
                await self._set_status(session, row, "failed", error_code="backup_already_running")
                await session.flush()
                raise HTTPException(status_code=409, detail={"code": "backup_already_running"})

            await self._assert_tool_compatibility(session)
            await self._set_status(session, row, "dumping_database", pg_dump_version=self.tools.pg_dump_version, pg_restore_version=self.tools.pg_restore_version)
            files_dir.mkdir(parents=True, exist_ok=True)
            dump_result = await asyncio.to_thread(
                PostgreSQLDumpService(target=self.target, tools=self.tools, timeout_seconds=self.settings.backup_command_timeout_seconds).run_dump,
                dump_path,
            )
            await self._set_status(session, row, "collecting_files")
            file_manifest = await FileSnapshotService(
                upload_root=self.settings.resolved_upload_dir,
                storage=FileStorage(),
            ).collect(session, files_dir)
            await self._set_status(session, row, "building_manifest")
            manifest = self._build_manifest(row, dump_result, file_manifest)
            self._write_bundle_metadata(files_dir, manifest)
            with tarfile.open(plaintext_bundle, "w:gz") as archive:
                for item in files_dir.rglob("*"):
                    if item.is_file():
                        archive.add(item, arcname=item.relative_to(files_dir).as_posix())
            await self._set_status(session, row, "encrypting")
            encryption_result = await asyncio.to_thread(self.encryption.encrypt_file, plaintext_bundle, encrypted_path)
            await self._set_status(
                session,
                row,
                "verifying_local_bundle",
                **encryption_result,
                encrypted_bundle_key=encrypted_name,
                encrypted_bundle_sha256=encryption_result["encrypted_sha256"],
                encrypted_checksum=encryption_result["encrypted_sha256"],
                size_bytes=encryption_result["encrypted_size"],
            )
            if not encrypted_path.is_file() or encrypted_path.stat().st_size <= 0:
                raise RuntimeError("encrypted_bundle_missing")
            await self._set_status(session, row, "uploading_offsite")
            offsite_result = await asyncio.to_thread(self.offsite.upload_and_verify, encrypted_path)
            await self._set_status(session, row, "verifying_offsite_copy", **offsite_result)
            restore_result: dict[str, Any] = {}
            if self.settings.backup_require_restore_verification:
                await self._set_status(session, row, "restoring_test_database")
                restore_result = await BackupRestoreVerificationService(
                    target=self.target,
                    tools=self.tools,
                    timeout_seconds=self.settings.backup_command_timeout_seconds,
                ).verify(encrypted_path, self.encryption)
                await self._set_status(session, row, "verifying_restored_files", **restore_result)
            # The database enforces that a ready backup already has a
            # non-empty bundle path. Set it before the constrained status
            # transition, otherwise the transition is rejected even after
            # dump, encryption, offsite copy, and restore verification pass.
            row.path = str(encrypted_path)
            await self._set_status(
                session,
                row,
                "ready",
                encrypted_bundle_key=encrypted_name,
                encrypted_bundle_sha256=encryption_result["encrypted_sha256"],
                encrypted_checksum=encryption_result["encrypted_sha256"],
                size_bytes=encryption_result["encrypted_size"],
                verification_status="verified",
                restore_verification_status=restore_result.get("restore_verification_status", "not_required"),
            )
            await self.retention.apply(session)
            return self._response(row)
        except HTTPException:
            raise
        except Exception as exc:
            await self._set_status(session, row, "failed", error_code=str(exc).splitlines()[0][:200])
            row.path = str(encrypted_path) if encrypted_path.exists() else ""
            return self._response(row)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            if lock_conn is not None:
                if acquired:
                    await lock_conn.execute(text("select pg_advisory_unlock(:key)"), {"key": BACKUP_LOCK_KEY})
                await lock_conn.close()

    async def _assert_tool_compatibility(self, session: AsyncSession) -> None:
        server_version = str((await session.execute(text("show server_version_num"))).scalar_one())
        dump_major = self._major(self.tools.pg_dump_version)
        server_major = int(server_version) // 10000
        if dump_major is not None and dump_major < server_major:
            raise RuntimeError("backup_tool_version_incompatible")

    @staticmethod
    def _major(version: str) -> int | None:
        match = re.search(r"(\d+)(?:\.\d+)?", version)
        return int(match.group(1)) if match else None

    def _build_manifest(self, row: Any, dump_result: dict[str, Any], file_manifest: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "backup_id": str(row.id),
            "format_version": "luxury-secure-backup-v1",
            "created_at": now.isoformat(),
            "source": {"application": "luxury", "environment": self.settings.app_env, "database": self.settings.database_name},
            "database": {
                "dump_file": "database.dump",
                "format": "custom",
                "size": dump_result["dump_size"],
                "sha256": dump_result["dump_sha256"],
                "pg_dump_version": self.tools.pg_dump_version,
                "pg_restore_version": self.tools.pg_restore_version,
            },
            "files": file_manifest,
            "encryption": {"algorithm": "Fernet-AES128-CBC-HMAC-SHA256", "key_id": self.encryption.key_id},
            "restore_instructions": {"requires_decryption_key": True, "database_dump": "database.dump", "no_credentials_included": True},
        }

    @staticmethod
    def _write_bundle_metadata(bundle_dir: Path, manifest: dict[str, Any]) -> None:
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (bundle_dir / "database-metadata.json").write_text(json.dumps(manifest["database"], ensure_ascii=False, indent=2), encoding="utf-8")
        (bundle_dir / "file-manifest.json").write_text(json.dumps(manifest["files"], ensure_ascii=False, indent=2), encoding="utf-8")
        checksums = []
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file() and path.name != "checksums.sha256":
                checksums.append(f"{_sha256(path)}  {path.relative_to(bundle_dir).as_posix()}")
        (bundle_dir / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    @staticmethod
    def _response(row: Any) -> dict[str, Any]:
        data = serialize_record(row)
        extra = row.extra_data or {}
        downloadable = BackupCoordinator._is_downloadable(row)
        data.update(
            {
                "id": str(row.id),
                "status": row.status,
                "encrypted": bool(extra.get("encrypted_bundle_key")),
                "download_url": f"/backups/{row.id}/download" if downloadable else None,
                "ready_has_verified_restore": extra.get("restore_verification_status") in {"verified", "not_required"},
                "offsite_status": extra.get("offsite_status"),
                "verification_status": extra.get("verification_status"),
            }
        )
        return data

    @staticmethod
    def _is_downloadable(row: Any) -> bool:
        extra = row.extra_data or {}
        try:
            size_bytes = int(extra.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        return (
            row.status == "ready"
            and bool(extra.get("encrypted_bundle_key"))
            and bool(extra.get("encrypted_checksum"))
            and size_bytes > 0
            and extra.get("verification_status") == "verified"
            and extra.get("offsite_status") == "verified"
            and extra.get("restore_verification_status") in {"verified", "not_required"}
        )

    async def list_backups(self, session: AsyncSession) -> list[dict[str, Any]]:
        model = MODEL_BY_TABLE["backup_records"]
        rows = (
            await session.execute(
                text(
                    """
                    select id from backup_records
                    where deleted_at is null
                    order by created_at desc
                    limit 500
                    """
                )
            )
        ).scalars().all()
        result = []
        for row_id in rows:
            row = await session.get(model, row_id)
            if row is not None:
                result.append(self._response(row))
        return result

    async def download(self, session: AsyncSession, backup_id: uuid.UUID) -> FileResponse:
        row = await session.get(MODEL_BY_TABLE["backup_records"], backup_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="backup_not_found")
        if row.status != "ready":
            raise HTTPException(status_code=409, detail="backup_not_ready")
        extra = row.extra_data or {}
        if not self._is_downloadable(row):
            raise HTTPException(status_code=409, detail="backup_not_verified")
        encrypted_key = str(extra.get("encrypted_bundle_key") or "")
        target = (self.settings.resolved_backup_storage_dir / encrypted_key).resolve()
        if target != self.settings.resolved_backup_storage_dir and self.settings.resolved_backup_storage_dir not in target.parents:
            raise HTTPException(status_code=404, detail="backup_not_found")
        cleanup_target = False
        if not target.is_file() or target.stat().st_size <= 0:
            offsite_key = str(extra.get("offsite_key") or "").strip()
            expected_size = int(extra.get("encrypted_size") or extra.get("size_bytes") or 0)
            expected_sha256 = str(extra.get("encrypted_checksum") or extra.get("encrypted_bundle_sha256") or "")
            if not offsite_key or expected_size <= 0 or not expected_sha256:
                raise HTTPException(status_code=404, detail="backup_file_not_found")
            handle, temporary_name = tempfile.mkstemp(prefix="luxury-backup-download-", suffix=".fernet")
            os.close(handle)
            target = Path(temporary_name)
            try:
                await asyncio.to_thread(
                    self.offsite.download_and_verify,
                    offsite_key,
                    target,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
            except Exception as exc:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=404, detail="backup_file_not_found") from exc
            cleanup_target = True
        return FileResponse(
            target,
            filename=target.name,
            media_type="application/octet-stream",
            background=BackgroundTask(target.unlink, missing_ok=True) if cleanup_target else None,
        )


def backup_static_audit() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    matches: list[dict[str, Any]] = []
    hardcoded_pg18 = "Program Files" + "\\PostgreSQL\\18"
    for path in root.rglob("*.py"):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        text_value = path.read_text(encoding="utf-8", errors="ignore")
        has_pg_url_process_risk = bool(
            re.search(
                r"subprocess\.(?:run|Popen)\([^)]*to_sync_url\([^)]*(?:database_url|restore_url)",
                text_value,
                re.DOTALL,
            )
        )
        if hardcoded_pg18 in text_value or has_pg_url_process_risk:
            matches.append({"file": str(path), "hardcoded_pg18": hardcoded_pg18 in text_value, "database_url_near_pg_tool": has_pg_url_process_risk})
    return {"hardcoded_postgresql_18_paths": sum(1 for item in matches if item["hardcoded_pg18"]), "findings": matches}
