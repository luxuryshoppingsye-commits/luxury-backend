from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_postgres import (  # noqa: E402
    PgCommandTarget,
    TemporaryPgPassFile,
    connect,
    database_snapshot,
    find_pg_tool,
    load_env_files,
    public_tables,
    query_all,
    query_one,
    run_command,
    safe_db_info,
    sha256_file,
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value.normalize()) if value == value.to_integral() else str(value)
    if isinstance(value, dict):
        return {str(key): canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def run_pg_dump(database_url: str, output_path: Path) -> dict[str, Any]:
    target = PgCommandTarget.from_url(database_url)
    args = [
        find_pg_tool("pg_dump"),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
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
        str(output_path),
    ]
    with TemporaryPgPassFile(target) as pgpass:
        result = run_command(args, env={"PGPASSFILE": str(pgpass)})
    return {
        "ok": result.returncode == 0,
        "duration_seconds": result.duration_seconds,
        "file": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
    }


def run_psql(database_url: str, sql: str) -> dict[str, Any]:
    target = PgCommandTarget.from_url(database_url)
    with TemporaryPgPassFile(target) as pgpass:
        result = run_command(
            [
                find_pg_tool("psql"),
                "--host",
                target.host,
                "--port",
                str(target.port),
                "--username",
                target.username,
                "--dbname",
                target.database,
                "--no-password",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            env={"PGPASSFILE": str(pgpass)},
        )
    return {
        "ok": result.returncode == 0,
        "duration_seconds": result.duration_seconds,
        "stdout_tail": "\n".join((result.stdout or "").splitlines()[-20:]),
        "stderr_tail": "\n".join((result.stderr or "").splitlines()[-20:]),
    }


def run_pg_restore(database_url: str, dump_path: Path) -> dict[str, Any]:
    target = PgCommandTarget.from_url(database_url)
    args = [
        find_pg_tool("pg_restore"),
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.username,
        "--dbname",
        target.database,
        str(dump_path),
    ]
    with TemporaryPgPassFile(target) as pgpass:
        result = run_command(args, env={"PGPASSFILE": str(pgpass)})
    return {
        "ok": result.returncode == 0,
        "duration_seconds": result.duration_seconds,
        "stdout_tail": "\n".join((result.stdout or "").splitlines()[-20:]),
        "stderr_tail": "\n".join((result.stderr or "").splitlines()[-20:]),
    }


def connect_count(database_url: str) -> int:
    from backup_postgres import connect

    with connect(database_url) as conn:
        row = query_one(conn, "SELECT COUNT(*)::int AS count FROM information_schema.tables WHERE table_schema='public'")
    return int(row["count"]) if row else 0


def logical_database_fingerprint(database_url: str, output_path: Path) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    total_rows = 0
    with connect(database_url) as conn:
        for table in public_tables(conn):
            rows = query_all(conn, f'SELECT * FROM public."{table}"')
            encoded_rows = [
                json.dumps(canonical(dict(row)), ensure_ascii=False, sort_keys=True, default=str)
                for row in rows
            ]
            encoded_rows.sort()
            digest = __import__("hashlib").sha256()
            for row in encoded_rows:
                digest.update(row.encode("utf-8"))
                digest.update(b"\n")
            tables[table] = {"row_count": len(rows), "logical_hash": digest.hexdigest()}
            total_rows += len(rows)
    payload = {"table_count": len(tables), "total_rows": total_rows, "tables": tables}
    write_json(output_path, payload)
    return payload


def compare_snapshots(source: dict[str, Any], target: dict[str, Any], source_logical: dict[str, Any], target_logical: dict[str, Any]) -> dict[str, Any]:
    source_tables = source.get("tables", {})
    target_tables = target.get("tables", {})
    source_logical_tables = source_logical.get("tables", {})
    target_logical_tables = target_logical.get("tables", {})
    table_names = sorted(set(source_tables) | set(target_tables))
    mismatches: list[dict[str, Any]] = []
    for table in table_names:
        source_table = source_tables.get(table)
        target_table = target_tables.get(table)
        source_logical_table = source_logical_tables.get(table, {})
        target_logical_table = target_logical_tables.get(table, {})
        if source_table is None or target_table is None:
            mismatches.append({"table": table, "issue": "missing_table", "source_exists": source_table is not None, "target_exists": target_table is not None})
            continue
        checks = {
            "row_count": (source_table.get("row_count"), target_table.get("row_count")),
            "logical_hash": (source_logical_table.get("logical_hash"), target_logical_table.get("logical_hash")),
            "numeric_sums": (source_table.get("numeric_sums"), target_table.get("numeric_sums")),
        }
        diffs = {key: {"source": left, "target": right} for key, (left, right) in checks.items() if left != right}
        if diffs:
            mismatches.append({"table": table, "issue": "data_mismatch", "diffs": diffs})
    return {
        "ok": not mismatches,
        "source_table_count": len(source_tables),
        "target_table_count": len(target_tables),
        "source_total_rows": source.get("total_rows"),
        "target_total_rows": target.get("total_rows"),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    status = "PASS" if result["ok"] else "FAIL"
    comparison = result["comparison"]
    lines = [
        "# Neon Database Upload Report",
        "",
        f"Generated at: {result['generated_at']}",
        "",
        "## Target",
        "",
        f"- Host: `{result['target_database']['host']}`",
        f"- Port: `{result['target_database']['port']}`",
        f"- Database: `{result['target_database']['database']}`",
        "- Password printed: `false`",
        "",
        "## Backups",
        "",
        f"- Local source dump: `{result['local_source_dump']['file']}`",
        f"- Local source dump SHA-256: `{result['local_source_dump']['sha256']}`",
        f"- Neon backup before restore: `{result['neon_before_backup'].get('file')}`",
        f"- Neon backup before restore SHA-256: `{result['neon_before_backup'].get('sha256')}`",
        "",
        "## Restore",
        "",
        f"- Reset target schema: `{result['reset_schema']['ok']}`",
        f"- Restore to Neon: `{result['restore']['ok']}`",
        f"- Analyze: `{result['analyze']['ok']}`",
        "",
        "## Comparison",
        "",
        f"- Source tables: `{comparison['source_table_count']}`",
        f"- Target tables: `{comparison['target_table_count']}`",
        f"- Source rows: `{comparison['source_total_rows']}`",
        f"- Target rows: `{comparison['target_total_rows']}`",
        f"- Mismatch count: `{comparison['mismatch_count']}`",
        "",
        f"{status}: Neon database restore {'matched the local PostgreSQL source' if result['ok'] else 'did not fully match the local PostgreSQL source'}.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Upload local PostgreSQL database to Neon and verify parity.")
    parser.add_argument("--source-database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--target-database-url", default=os.environ.get("NEON_DATABASE_URL"))
    parser.add_argument("--result-dir", default=str(BACKEND_DIR / "data" / "neon_upload"))
    parser.add_argument("--run-id", default=os.environ.get("NEON_UPLOAD_RUN_ID") or utc_stamp())
    args = parser.parse_args()

    if not args.source_database_url:
        raise SystemExit("DATABASE_URL is required.")
    if not args.target_database_url:
        raise SystemExit("NEON_DATABASE_URL is required.")
    if os.environ.get("ALLOW_NEON_UPLOAD_DESTRUCTIVE") != "true":
        raise SystemExit("Refusing destructive Neon upload without ALLOW_NEON_UPLOAD_DESTRUCTIVE=true.")

    started = time.perf_counter()
    result_dir = Path(args.result_dir).resolve() / args.run_id
    result_dir.mkdir(parents=True, exist_ok=True)
    source_dump = result_dir / "local-source.dump"
    neon_before_dump = result_dir / "neon-before-restore.dump"

    source_snapshot = database_snapshot(args.source_database_url, result_dir / "source_snapshot.json")
    neon_before_backup = run_pg_dump(args.target_database_url, neon_before_dump)
    local_source_dump = run_pg_dump(args.source_database_url, source_dump)
    reset_schema = run_psql(
        args.target_database_url,
        """
        DROP SCHEMA IF EXISTS public CASCADE;
        CREATE SCHEMA public;
        GRANT ALL ON SCHEMA public TO public;
        DO $$
        BEGIN
          EXECUTE format('ALTER DATABASE %I SET search_path TO public', current_database());
          EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path TO public', current_user, current_database());
        END $$;
        SET search_path TO public;
        """,
    )
    restore = run_pg_restore(args.target_database_url, source_dump)
    analyze = run_psql(args.target_database_url, "ANALYZE;")
    target_snapshot = database_snapshot(args.target_database_url, result_dir / "target_snapshot.json")
    source_logical = logical_database_fingerprint(args.source_database_url, result_dir / "source_logical_fingerprint.json")
    target_logical = logical_database_fingerprint(args.target_database_url, result_dir / "target_logical_fingerprint.json")
    comparison = compare_snapshots(source_snapshot, target_snapshot, source_logical, target_logical)

    result = {
        "generated_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "result_dir": str(result_dir),
        "source_database": safe_db_info(args.source_database_url),
        "target_database": safe_db_info(args.target_database_url),
        "target_table_count_before_restore": connect_count(args.target_database_url),
        "source_snapshot": str(result_dir / "source_snapshot.json"),
        "target_snapshot": str(result_dir / "target_snapshot.json"),
        "source_logical_fingerprint": str(result_dir / "source_logical_fingerprint.json"),
        "target_logical_fingerprint": str(result_dir / "target_logical_fingerprint.json"),
        "neon_before_backup": neon_before_backup,
        "local_source_dump": local_source_dump,
        "reset_schema": reset_schema,
        "restore": restore,
        "analyze": analyze,
        "comparison": comparison,
        "ok": comparison["ok"] and reset_schema["ok"] and restore["ok"] and analyze["ok"],
    }
    plaintext_cleanup = []
    for dump_file in (source_dump, neon_before_dump):
        removed = False
        if dump_file.exists():
            dump_file.unlink()
            removed = True
        plaintext_cleanup.append({"file": dump_file.name, "removed": removed})
    result["plaintext_cleanup"] = plaintext_cleanup
    write_json(result_dir / "neon_upload_result.json", result)
    write_report(PROJECT_DIR / "NEON_DATABASE_UPLOAD_REPORT.md", result)
    print(json.dumps({"ok": result["ok"], "result_dir": str(result_dir), "report": str(PROJECT_DIR / "NEON_DATABASE_UPLOAD_REPORT.md")}, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
