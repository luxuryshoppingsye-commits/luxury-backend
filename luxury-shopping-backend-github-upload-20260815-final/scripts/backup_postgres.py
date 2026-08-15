from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent
RESTORE_DB_PREFIX = "luxury_shopping_restore_test_"
SKIP_UPLOAD_PARTS = {
    ".env",
    "__pycache__",
    "backups",
    "backup_restore",
    "restore_test_uploads",
    ".pytest_cache",
}
FINANCIAL_COLUMNS = {
    "amount",
    "total",
    "subtotal",
    "discount_total",
    "shipping_total",
    "balance",
    "fee",
    "price",
    "original_price",
    "unit_price",
    "total_price",
    "quantity",
    "stock_quantity",
}


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass(frozen=True)
class PgCommandTarget:
    host: str
    port: int
    username: str
    password: str
    database: str

    @classmethod
    def from_url(cls, url: str) -> "PgCommandTarget":
        parsed = urlsplit(to_sync_url(url))
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise RuntimeError("PostgreSQL URL is required")
        database = unquote(parsed.path.lstrip("/"))
        if not database:
            raise RuntimeError("PostgreSQL database name is required")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=int(parsed.port or 5432),
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=database,
        )


class TemporaryPgPassFile:
    def __init__(self, target: PgCommandTarget) -> None:
        self.target = target
        root = Path(tempfile.gettempdir()) / "luxury-runtime-secrets"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"pgpass-{uuid.uuid4().hex}.conf"

    def __enter__(self) -> Path:
        self.path.write_text(
            f"{self.target.host}:{self.target.port}:*:{self.target.username}:{self.target.password}\n",
            encoding="utf-8",
        )
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(
                    ["icacls", str(self.path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True,
                    shell=False,
                )
        return self.path

    def __exit__(self, *_: object) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_files() -> None:
    for env_path in (PROJECT_DIR / ".env", BACKEND_DIR / ".env"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def to_sync_url(url: str) -> str:
    value = url.strip()
    if value.startswith("postgresql+asyncpg://"):
        return value.replace("postgresql+asyncpg://", "postgresql://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql://", 1)
    return value


def to_async_url(url: str) -> str:
    value = url.strip()
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    return value


def database_name(url: str) -> str:
    return unquote(urlsplit(to_sync_url(url)).path.lstrip("/"))


def replace_database(url: str, database: str) -> str:
    parsed = urlsplit(to_sync_url(url))
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{quote(database)}", parsed.query, parsed.fragment))


def server_database_url(url: str) -> str:
    return replace_database(url, "postgres")


def safe_db_info(url: str) -> dict[str, Any]:
    parsed = urlsplit(to_sync_url(url))
    return {
        "scheme": parsed.scheme,
        "username": parsed.username or "",
        "host": parsed.hostname or "",
        "port": parsed.port,
        "database": database_name(url),
        "password_present": bool(parsed.password),
    }


def redact(text: str) -> str:
    if not text:
        return text
    return re.sub(r"(postgres(?:ql)?(?:\+asyncpg)?://[^:/@\s]+:)[^@/\s]+@", r"\1***@", text)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_pg_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidates: list[Path] = []
    for env_key in ("PG_BIN_DIR", "BACKUP_PG_BIN_DIR"):
        configured = os.environ.get(env_key)
        if configured:
            candidates.append(Path(configured))
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            result = subprocess.run(
                [pg_config, "--bindir"],
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
                shell=False,
            )
            if result.stdout.strip():
                candidates.append(Path(result.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    if os.name == "nt":
        for root in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")), Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))):
            pg_root = root / "PostgreSQL"
            if not pg_root.is_dir():
                continue
            for child in sorted(pg_root.iterdir(), reverse=True):
                if child.is_dir():
                    candidates.append(child / "bin")
                    runtime = child / "pgAdmin 4" / "runtime"
                    if runtime.is_dir():
                        candidates.append(runtime)
    seen: set[Path] = set()
    exe_names = [f"{name}.exe", name] if os.name == "nt" else [name]
    for pg_bin in candidates:
        resolved = pg_bin.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for exe_name in exe_names:
            candidate = resolved / exe_name
            if candidate.exists():
                return str(candidate)
    raise FileNotFoundError(f"{name} was not found. Set PG_BIN_DIR or add PostgreSQL bin to PATH.")


def run_command(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> CommandResult:
    started = time.perf_counter()
    base_env = {key: value for key, value in os.environ.items() if key not in {"DATABASE_URL", "PGPASSWORD"}}
    clean_env = {key: value for key, value in (env or {}).items() if key not in {"DATABASE_URL", "PGPASSWORD"}}
    completed = subprocess.run(
        args,
        text=True,
        capture_output=True,
        env={**base_env, **clean_env},
        timeout=int(os.environ.get("BACKUP_COMMAND_TIMEOUT_SECONDS", "120")),
        shell=False,
    )
    result = CommandResult(
        args=args,
        returncode=completed.returncode,
        stdout=redact(completed.stdout or ""),
        stderr=redact(completed.stderr or ""),
        duration_seconds=round(time.perf_counter() - started, 3),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({Path(args[0]).name}, exit {result.returncode}): {result.stderr.strip()}")
    return result


def pg_version(tool: str) -> str:
    try:
        result = run_command([find_pg_tool(tool), "--version"], check=False)
        return (result.stdout or result.stderr).strip()
    except Exception as error:
        return f"unavailable: {error}"


def connect(url: str):
    return psycopg.connect(to_sync_url(url), row_factory=dict_row)


def query_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None


def query_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def public_tables(conn) -> list[str]:
    rows = query_all(
        conn,
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
    )
    return [row["table_name"] for row in rows]


def table_columns(conn, table: str) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )


def table_hash(conn, table: str) -> str:
    sql = f"""
        SELECT md5(COALESCE(string_agg(row_data, '' ORDER BY row_data), '')) AS hash
        FROM (
            SELECT to_jsonb(t)::text AS row_data
            FROM public.{quote_ident(table)} AS t
        ) AS rows
    """
    row = query_one(conn, sql)
    return str(row["hash"]) if row and row.get("hash") else hashlib.md5(b"").hexdigest()


def numeric_sums(conn, table: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for column in table_columns(conn, table):
        name = column["column_name"]
        data_type = str(column["data_type"])
        if name not in FINANCIAL_COLUMNS or data_type not in {"integer", "bigint", "smallint", "numeric", "real", "double precision"}:
            continue
        row = query_one(conn, f"SELECT COALESCE(SUM({quote_ident(name)}), 0)::text AS total FROM public.{quote_ident(table)}")
        sums[name] = str(row["total"] if row else "0")
    return sums


def database_snapshot(database_url: str, output_path: Path | None = None) -> dict[str, Any]:
    with connect(database_url) as conn:
        db_stats = query_one(
            conn,
            """
            SELECT current_database() AS database_name,
                   pg_database_size(current_database()) AS database_size_bytes,
                   version() AS postgres_version,
                   current_setting('server_encoding') AS encoding,
                   d.datcollate AS collation
            FROM pg_database d
            WHERE d.datname = current_database()
            """,
        ) or {}
        tables: dict[str, Any] = {}
        total_rows = 0
        for table in public_tables(conn):
            count_row = query_one(conn, f"SELECT COUNT(*)::bigint AS row_count FROM public.{quote_ident(table)}")
            row_count = int(count_row["row_count"] if count_row else 0)
            total_rows += row_count
            tables[table] = {"row_count": row_count, "hash": table_hash(conn, table), "sums": numeric_sums(conn, table)}
        sequences = query_all(
            conn,
            """
            SELECT schemaname, sequencename, last_value, start_value, increment_by
            FROM pg_sequences
            WHERE schemaname = 'public'
            ORDER BY sequencename
            """,
        )
        indexes = query_all(
            conn,
            """
            SELECT tablename, indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """,
        )
        constraints = query_all(
            conn,
            """
            SELECT conname, contype, rel.relname AS table_name
            FROM pg_constraint c
            JOIN pg_class rel ON rel.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY rel.relname, c.conname
            """,
        )
        alembic = None
        if "alembic_version" in tables:
            row = query_one(conn, "SELECT version_num FROM public.alembic_version LIMIT 1")
            alembic = row["version_num"] if row else None
    snapshot = {
        "created_at": utc_now(),
        "database": db_stats,
        "table_count": len(tables),
        "total_rows": total_rows,
        "tables": tables,
        "sequences": sequences,
        "indexes_count": len(indexes),
        "constraints_count": len(constraints),
        "indexes": indexes,
        "constraints": constraints,
        "alembic_version": alembic,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return snapshot


def should_skip_upload(path: Path) -> bool:
    return any(part in SKIP_UPLOAD_PARTS for part in path.parts)


def upload_manifest(upload_dir: Path, output_path: Path | None = None) -> dict[str, Any]:
    upload_dir = upload_dir.resolve()
    files: list[dict[str, Any]] = []
    total_bytes = 0
    if upload_dir.exists():
        for path in sorted(upload_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(upload_dir)
            if should_skip_upload(rel):
                continue
            size = path.stat().st_size
            total_bytes += size
            files.append(
                {
                    "path": rel.as_posix(),
                    "size": size,
                    "sha256": sha256_file(path),
                    "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    manifest = {"root": str(upload_dir), "file_count": len(files), "total_bytes": total_bytes, "files": files}
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def create_uploads_archive(upload_dir: Path, archive_path: Path) -> dict[str, Any]:
    manifest = upload_manifest(upload_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in manifest["files"]:
            archive.write(upload_dir / item["path"], item["path"])
    return {
        "path": str(archive_path),
        "size": archive_path.stat().st_size,
        "sha256": sha256_file(archive_path),
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }


def extract_uploads_archive(archive_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe upload archive path: {member.filename}")
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Unsafe upload archive target: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


def compare_upload_manifests(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_files = {item["path"]: item for item in before.get("files", [])}
    after_files = {item["path"]: item for item in after.get("files", [])}
    mismatches: list[dict[str, Any]] = []
    for path, item in before_files.items():
        other = after_files.get(path)
        if other is None:
            mismatches.append({"path": path, "issue": "missing_after"})
        elif item["size"] != other["size"] or item["sha256"] != other["sha256"]:
            mismatches.append({"path": path, "issue": "content_mismatch", "before": item, "after": other})
    for path in sorted(set(after_files) - set(before_files)):
        mismatches.append({"path": path, "issue": "extra_after"})
    return {"before_count": len(before_files), "after_count": len(after_files), "mismatch_count": len(mismatches), "mismatches": mismatches[:200], "ok": not mismatches}


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    before_tables = before.get("tables", {})
    after_tables = after.get("tables", {})
    for table, data in before_tables.items():
        restored = after_tables.get(table)
        if restored is None:
            mismatches.append({"table": table, "issue": "missing_after"})
            continue
        if data.get("row_count") != restored.get("row_count"):
            mismatches.append({"table": table, "issue": "row_count", "before": data.get("row_count"), "after": restored.get("row_count")})
        if data.get("hash") != restored.get("hash"):
            mismatches.append({"table": table, "issue": "hash", "before": data.get("hash"), "after": restored.get("hash")})
        if data.get("sums") != restored.get("sums"):
            mismatches.append({"table": table, "issue": "financial_sums", "before": data.get("sums"), "after": restored.get("sums")})
    for table in sorted(set(after_tables) - set(before_tables)):
        mismatches.append({"table": table, "issue": "extra_after"})
    return {
        "table_count_before": len(before_tables),
        "table_count_after": len(after_tables),
        "total_rows_before": before.get("total_rows"),
        "total_rows_after": after.get("total_rows"),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:200],
        "ok": not mismatches,
    }


def relationship_checks(database_url: str) -> dict[str, Any]:
    checks = {
        "profiles_without_user": "SELECT COUNT(*) AS count FROM profiles p LEFT JOIN users u ON u.id = p.user_id WHERE u.id IS NULL",
        "orders_without_user": "SELECT COUNT(*) AS count FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.id IS NULL",
        "order_items_without_order": "SELECT COUNT(*) AS count FROM order_items i LEFT JOIN orders o ON o.id = i.order_id WHERE o.id IS NULL",
        "variants_without_product": "SELECT COUNT(*) AS count FROM product_variants v LEFT JOIN products p ON p.id = v.product_id WHERE p.id IS NULL",
        "cart_without_user": "SELECT COUNT(*) AS count FROM user_cart c LEFT JOIN users u ON u.id = c.user_id WHERE u.id IS NULL",
        "wishlist_without_user": "SELECT COUNT(*) AS count FROM wishlist w LEFT JOIN users u ON u.id = w.user_id WHERE u.id IS NULL",
        "payments_without_order": "SELECT COUNT(*) AS count FROM payments p LEFT JOIN orders o ON o.id = p.order_id WHERE o.id IS NULL",
        "payment_receipts_without_order": "SELECT COUNT(*) AS count FROM payment_receipts p LEFT JOIN orders o ON o.id = p.order_id WHERE o.id IS NULL",
        "ticket_messages_without_ticket": "SELECT COUNT(*) AS count FROM ticket_messages m LEFT JOIN support_tickets t ON t.id = m.ticket_id WHERE t.id IS NULL",
    }
    results: dict[str, Any] = {}
    with connect(database_url) as conn:
        tables = set(public_tables(conn))
        for name, sql in checks.items():
            used_tables = {token for token in re.findall(r"\b[a-z_]+\b", sql) if token in checks or token in tables}
            if not used_tables.issubset(tables):
                results[name] = {"skipped": True, "count": None}
                continue
            row = query_one(conn, sql)
            results[name] = {"skipped": False, "count": int(row["count"]) if row else 0}
    failures = {name: value for name, value in results.items() if not value.get("skipped") and value.get("count") != 0}
    return {"checks": results, "failure_count": len(failures), "failures": failures, "ok": not failures}


def assert_safe_restore_target(source_url: str, restore_url: str) -> None:
    source = safe_db_info(source_url)
    target = safe_db_info(restore_url)
    if source["host"] == target["host"] and source["port"] == target["port"] and source["database"] == target["database"]:
        raise RuntimeError("Refusing to restore over the source database.")
    target_name = str(target["database"])
    if not target_name.startswith(RESTORE_DB_PREFIX):
        raise RuntimeError(f"Restore database must start with {RESTORE_DB_PREFIX!r}.")
    if re.search(r"\b(prod|production)\b", target_name, flags=re.IGNORECASE):
        raise RuntimeError("Refusing to restore into a database name that looks like production.")


def create_database_if_needed(source_url: str, restore_url: str) -> None:
    assert_safe_restore_target(source_url, restore_url)
    target_db = database_name(restore_url)
    with connect(server_database_url(source_url)) as conn:
        conn.autocommit = True
        exists = query_one(conn, "SELECT 1 AS ok FROM pg_database WHERE datname = %s", (target_db,))
        if exists:
            raise RuntimeError(f"Restore database already exists: {target_db}")
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {quote_ident(target_db)}")


def drop_restore_database(source_url: str, restore_url: str) -> None:
    assert_safe_restore_target(source_url, restore_url)
    target_db = database_name(restore_url)
    with connect(server_database_url(source_url)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (target_db,))
            cursor.execute(f"DROP DATABASE IF EXISTS {quote_ident(target_db)}")


def database_metadata(database_url: str) -> dict[str, Any]:
    with connect(database_url) as conn:
        row = query_one(
            conn,
            """
            SELECT version() AS postgres_server_version,
                   current_database() AS database_name,
                   pg_database_size(current_database()) AS database_size_bytes,
                   current_setting('server_encoding') AS encoding,
                   d.datcollate AS collation,
                   d.datctype AS ctype
            FROM pg_database d
            WHERE d.datname = current_database()
            """,
        ) or {}
        tables = public_tables(conn)
        alembic = None
        if "alembic_version" in tables:
            version = query_one(conn, "SELECT version_num FROM alembic_version LIMIT 1")
            alembic = version["version_num"] if version else None
    return {**row, "table_count": len(tables), "alembic_version": alembic}


def git_commit_hash() -> str | None:
    if not (PROJECT_DIR / ".git").exists():
        return None
    try:
        result = run_command(["git", "rev-parse", "HEAD"], check=False)
        return result.stdout.strip() or None
    except Exception:
        return None


def run_pg_dump(database_url: str, dump_path: Path) -> CommandResult:
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    target = PgCommandTarget.from_url(database_url)
    with TemporaryPgPassFile(target) as pgpass:
        return run_command(
            [
                find_pg_tool("pg_dump"),
                "--format=custom",
                "--verbose",
                "--no-password",
                "--host",
                target.host,
                "--port",
                str(target.port),
                "--username",
                target.username,
                "--dbname",
                target.database,
                "--file",
                str(dump_path),
            ],
            env={"PGPASSFILE": str(pgpass)},
        )


def run_pg_restore_list(dump_path: Path, output_path: Path | None = None) -> CommandResult:
    result = run_command([find_pg_tool("pg_restore"), "--list", str(dump_path)])
    if output_path:
        output_path.write_text(result.stdout, encoding="utf-8")
    return result


def run_pg_restore(restore_url: str, dump_path: Path, *, no_owner: bool = True) -> CommandResult:
    target = PgCommandTarget.from_url(restore_url)
    args = [
        find_pg_tool("pg_restore"),
        "--verbose",
        "--exit-on-error",
        "--no-password",
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.username,
    ]
    if no_owner:
        args.append("--no-owner")
    args.extend(["--dbname", target.database, str(dump_path)])
    with TemporaryPgPassFile(target) as pgpass:
        return run_command(args, env={"PGPASSFILE": str(pgpass)})


def create_backup_package(
    *,
    database_url: str,
    backup_dir: Path,
    upload_dir: Path,
    created_by: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    if os.environ.get("ALLOW_LEGACY_PLAINTEXT_BACKUP") != "true":
        raise RuntimeError("legacy_plaintext_backup_disabled_use_fastapi_encrypted_backup")
    started = time.perf_counter()
    backup_id = run_id or str(uuid.uuid4())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    package_dir = backup_dir / f"backup-{stamp}-{backup_id}"
    package_dir.mkdir(parents=True, exist_ok=True)
    pre_snapshot_path = package_dir / "backup_pre_restore_snapshot.json"
    uploads_manifest_path = package_dir / "uploads_manifest_before.json"
    dump_path = package_dir / f"postgres-{stamp}-{backup_id}.dump"
    uploads_archive_path = package_dir / f"uploads-{stamp}-{backup_id}.zip"
    restore_list_path = package_dir / "pg_restore_list.txt"

    pre_snapshot = database_snapshot(database_url, pre_snapshot_path)
    upload_manifest(upload_dir, uploads_manifest_path)
    dump_result = run_pg_dump(database_url, dump_path)
    uploads_archive = create_uploads_archive(upload_dir, uploads_archive_path)
    restore_list_result = run_pg_restore_list(dump_path, restore_list_path)
    disk = shutil.disk_usage(package_dir)
    metadata = database_metadata(database_url)
    manifest = {
        "backup_id": backup_id,
        "created_at": utc_now(),
        "created_by": created_by,
        "source_database": safe_db_info(database_url),
        "type": "postgres_custom_plus_uploads_zip",
        "package_dir": str(package_dir),
        "postgres_dump": {"name": dump_path.name, "path": str(dump_path), "size": dump_path.stat().st_size, "sha256": sha256_file(dump_path)},
        "uploads_archive": uploads_archive,
        "pre_snapshot": str(pre_snapshot_path),
        "uploads_manifest_before": str(uploads_manifest_path),
        "pg_restore_list": str(restore_list_path),
        "pg_restore_list_ok": restore_list_result.returncode == 0,
        "pg_dump_duration_seconds": dump_result.duration_seconds,
        "pg_restore_list_duration_seconds": restore_list_result.duration_seconds,
        "database_snapshot": {
            "table_count": pre_snapshot["table_count"],
            "total_rows": pre_snapshot["total_rows"],
            "database_size_bytes": pre_snapshot["database"].get("database_size_bytes"),
            "alembic_version": pre_snapshot.get("alembic_version"),
        },
        "environment": {
            "platform": platform.platform(),
            "postgres_server_version": metadata.get("postgres_server_version"),
            "pg_dump_version": pg_version("pg_dump"),
            "pg_restore_version": pg_version("pg_restore"),
            "git_commit": git_commit_hash(),
            "disk_free_bytes": disk.free,
            "encoding": metadata.get("encoding"),
            "collation": metadata.get("collation"),
        },
        "duration_seconds": round(time.perf_counter() - started, 3),
        "status": "verified",
    }
    manifest_path = package_dir / "backup_manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest


def verify_backup_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_dir = manifest_path.parent
    checks: list[dict[str, Any]] = []
    for key in ("postgres_dump", "uploads_archive"):
        info = manifest.get(key, {})
        path = Path(info.get("path") or package_dir / info.get("name", ""))
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        actual_sha = sha256_file(path) if exists and path.is_file() else None
        checks.append(
            {
                "name": key,
                "path": str(path),
                "exists": exists,
                "size": size,
                "expected_sha256": info.get("sha256"),
                "actual_sha256": actual_sha,
                "ok": exists and size > 0 and actual_sha == info.get("sha256"),
            }
        )
    restore_list_ok = False
    restore_list_error = ""
    dump_path = Path(manifest.get("postgres_dump", {}).get("path") or "")
    if dump_path.exists():
        try:
            run_pg_restore_list(dump_path)
            restore_list_ok = True
        except Exception as error:
            restore_list_error = str(error)
    checks.append({"name": "pg_restore_list", "ok": restore_list_ok, "error": restore_list_error})
    archive_ok = False
    archive_error = ""
    uploads_path = Path(manifest.get("uploads_archive", {}).get("path") or "")
    if uploads_path.exists():
        try:
            with zipfile.ZipFile(uploads_path) as archive:
                archive.testzip()
            archive_ok = True
        except Exception as error:
            archive_error = str(error)
    checks.append({"name": "uploads_zip_readable", "ok": archive_ok, "error": archive_error})
    return {"manifest_path": str(manifest_path), "ok": all(item["ok"] for item in checks), "checks": checks, "manifest": manifest}


def restore_backup_package(
    *,
    manifest_path: Path,
    source_database_url: str,
    restore_database_url: str,
    restore_upload_dir: Path,
    no_owner: bool = True,
) -> dict[str, Any]:
    assert_safe_restore_target(source_database_url, restore_database_url)
    verification = verify_backup_manifest(manifest_path)
    if not verification["ok"]:
        raise RuntimeError("Backup package verification failed before restore.")
    manifest = verification["manifest"]
    dump_path = Path(manifest["postgres_dump"]["path"])
    uploads_archive_path = Path(manifest["uploads_archive"]["path"])
    started = time.perf_counter()
    create_database_if_needed(source_database_url, restore_database_url)
    restore_result = run_pg_restore(restore_database_url, dump_path, no_owner=no_owner)
    analyze_started = time.perf_counter()
    with connect(restore_database_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("ANALYZE")
    analyze_duration = round(time.perf_counter() - analyze_started, 3)
    extract_uploads_archive(uploads_archive_path, restore_upload_dir)
    post_snapshot_path = manifest_path.parent / "backup_post_restore_snapshot.json"
    post_upload_manifest_path = manifest_path.parent / "uploads_manifest_after.json"
    after_snapshot = database_snapshot(restore_database_url, post_snapshot_path)
    after_uploads = upload_manifest(restore_upload_dir, post_upload_manifest_path)
    before_snapshot = json.loads(Path(manifest["pre_snapshot"]).read_text(encoding="utf-8"))
    before_uploads = json.loads(Path(manifest["uploads_manifest_before"]).read_text(encoding="utf-8"))
    db_compare = compare_snapshots(before_snapshot, after_snapshot)
    upload_compare = compare_upload_manifests(before_uploads, after_uploads)
    relationships = relationship_checks(restore_database_url)
    result = {
        "restore_database": safe_db_info(restore_database_url),
        "restore_upload_dir": str(restore_upload_dir),
        "pg_restore_duration_seconds": restore_result.duration_seconds,
        "analyze_duration_seconds": analyze_duration,
        "total_restore_duration_seconds": round(time.perf_counter() - started, 3),
        "post_snapshot": str(post_snapshot_path),
        "post_upload_manifest": str(post_upload_manifest_path),
        "database_compare": db_compare,
        "uploads_compare": upload_compare,
        "relationship_checks": relationships,
        "ok": db_compare["ok"] and upload_compare["ok"] and relationships["ok"],
    }
    (manifest_path.parent / "restore_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def latest_manifest(backup_dir: Path) -> Path:
    candidates = sorted(backup_dir.rglob("backup_manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No backup_manifest.json files under {backup_dir}")
    return candidates[0]


def default_backup_dir() -> Path:
    return Path(os.environ.get("BACKUP_DIR", str(BACKEND_DIR / "data" / "backup_restore"))).resolve()


def default_upload_dir() -> Path:
    return Path(os.environ.get("UPLOAD_DIR", str(BACKEND_DIR / "data" / "uploads"))).resolve()


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Create a PostgreSQL + uploads backup package.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--backup-dir", default=str(default_backup_dir()))
    parser.add_argument("--upload-dir", default=str(default_upload_dir()))
    parser.add_argument("--created-by", default=os.environ.get("BACKUP_CREATED_BY", "local-admin"))
    parser.add_argument("--run-id", default=os.environ.get("BACKUP_RUN_ID"))
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    manifest = create_backup_package(
        database_url=args.database_url,
        backup_dir=Path(args.backup_dir),
        upload_dir=Path(args.upload_dir),
        created_by=args.created_by,
        run_id=args.run_id,
    )
    print(json.dumps({"ok": True, "manifest_path": manifest["manifest_path"], "backup_id": manifest["backup_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
