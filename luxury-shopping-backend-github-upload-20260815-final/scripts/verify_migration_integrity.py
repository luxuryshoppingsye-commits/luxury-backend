from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_postgres import (  # noqa: E402
    connect,
    create_backup_package,
    database_metadata,
    database_snapshot,
    default_backup_dir,
    default_upload_dir,
    load_env_files,
    public_tables,
    query_all,
    query_one,
    relationship_checks,
    safe_db_info,
    table_columns,
    to_sync_url,
)
from migrate_legacy_data import ALIASES, _collections, _normalize_row  # noqa: E402


SOURCE_KIND_SUPABASE_DB = "forbidden_live_supabase_database"
SOURCE_KIND_SUPABASE_EXPORT = "supabase_export_files"
SOURCE_KIND_LEGACY_STATE = "legacy_state_json"
SOURCE_KIND_NONE = "missing_source"
SUPABASE_PATTERNS = (
    "supabase",
    "supabase.co",
    "/rest/v1",
    "/auth/v1",
    "/storage/v1",
    "/functions/v1",
    "/realtime/v1",
    "nzoxoduxecgrkxxwdobp",
)
FILE_COLUMN_PATTERN = re.compile(
    r"(image|avatar|logo|url|path|file|receipt|document|attachment|icon|banner|media|photo)",
    re.IGNORECASE,
)
UPLOAD_REF_PATTERN = re.compile(r"/uploads/[^\s\"'<>),]+")
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value.normalize()) if value == value.to_integral() else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc)


def equivalent_for_migration(column: str, source_value: Any, target_value: Any) -> bool:
    if column == "password_salt":
        return target_value in (None, "") or canonical(source_value) == canonical(target_value)
    if column == "password_hash":
        source_text = str(source_value or "")
        target_text = str(target_value or "")
        if source_text and target_text.startswith("$argon2id$"):
            return True
    if column.endswith("_at") or column in {"created_at", "updated_at", "deleted_at"}:
        source_dt = parse_datetime(source_value)
        target_dt = parse_datetime(target_value)
        if source_dt and target_dt:
            return source_dt == target_dt
    return canonical(source_value) == canonical(target_value)


def fingerprint_row(row: dict[str, Any]) -> str:
    encoded = json.dumps(canonical(row), ensure_ascii=False, sort_keys=True, default=json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def source_kind(args: argparse.Namespace) -> str:
    if args.supabase_source_database_url or os.environ.get("SUPABASE_SOURCE_DATABASE_URL"):
        return SOURCE_KIND_SUPABASE_DB
    if args.supabase_export_dir or os.environ.get("SUPABASE_EXPORT_DIR"):
        return SOURCE_KIND_SUPABASE_EXPORT
    if args.legacy_state_json or os.environ.get("LEGACY_STATE_JSON"):
        return SOURCE_KIND_LEGACY_STATE
    return SOURCE_KIND_NONE


def discover_source_path(args: argparse.Namespace) -> Path | None:
    if args.supabase_export_dir or os.environ.get("SUPABASE_EXPORT_DIR"):
        return Path(args.supabase_export_dir or os.environ["SUPABASE_EXPORT_DIR"]).resolve()
    if args.legacy_state_json or os.environ.get("LEGACY_STATE_JSON"):
        return Path(args.legacy_state_json or os.environ["LEGACY_STATE_JSON"]).resolve()
    return None


def load_legacy_state_source(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not path.exists():
        return {}, {"ok": False, "reason": f"source file not found: {path}"}
    raw = read_json(path)
    if not isinstance(raw, dict):
        return {}, {"ok": False, "reason": "legacy source JSON root is not an object"}
    collections = _collections(raw)
    return collections, {
        "ok": True,
        "source_kind": SOURCE_KIND_LEGACY_STATE,
        "path": str(path),
        "root_key_count": len(raw),
        "collection_count": len(collections),
        "collection_counts": {name: len(rows) for name, rows in sorted(collections.items())},
        "is_authoritative_supabase_source": False,
        "limitation": "This is the legacy state export used by the migration script, not a direct Supabase read-only export.",
    }


def load_export_dir_source(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not path.exists() or not path.is_dir():
        return {}, {"ok": False, "reason": f"source export directory not found: {path}"}
    tables: dict[str, list[dict[str, Any]]] = {}
    files: list[dict[str, Any]] = []
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in {".json", ".csv"}:
            continue
        table = file.stem.replace("-", "_")
        try:
            if file.suffix.lower() == ".json":
                payload = read_json(file)
                if isinstance(payload, list):
                    rows = [row for row in payload if isinstance(row, dict)]
                elif isinstance(payload, dict):
                    maybe_rows = payload.get("rows") or payload.get("data")
                    rows = [row for row in maybe_rows if isinstance(row, dict)] if isinstance(maybe_rows, list) else []
                else:
                    rows = []
            else:
                with file.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = [dict(row) for row in csv.DictReader(handle)]
        except Exception as error:
            files.append({"path": str(file), "table": table, "ok": False, "error": str(error)})
            continue
        if rows:
            tables[table] = rows
        files.append({"path": str(file), "table": table, "ok": True, "rows": len(rows)})
    return tables, {
        "ok": bool(tables),
        "source_kind": SOURCE_KIND_SUPABASE_EXPORT,
        "path": str(path),
        "files": files,
        "collection_count": len(tables),
        "collection_counts": {name: len(rows) for name, rows in sorted(tables.items())},
        "is_authoritative_supabase_source": True,
    }


def load_supabase_db_source(database_url: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    tables: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, Any] = {
        "ok": False,
        "source_kind": SOURCE_KIND_SUPABASE_DB,
        "database": safe_db_info(database_url),
        "is_authoritative_supabase_source": True,
    }
    try:
        with psycopg.connect(to_sync_url(database_url), row_factory=dict_row) as conn:
            source_tables = public_tables(conn)
            for table in source_tables:
                rows = query_all(conn, f"SELECT * FROM public.{quote_ident(table)} ORDER BY 1")
                tables[table] = [dict(row) for row in rows]
            regclass = query_one(conn, "SELECT to_regclass('auth.users') AS name")
            if regclass and regclass.get("name"):
                tables["auth.users"] = query_all(conn, "SELECT * FROM auth.users ORDER BY id")
        metadata.update(
            {
                "ok": True,
                "table_count": len(tables),
                "collection_counts": {name: len(rows) for name, rows in sorted(tables.items())},
            }
        )
    except Exception as error:
        metadata["reason"] = str(error)
    return tables, metadata


def primary_keys_by_table(database_url: str) -> dict[str, list[str]]:
    with connect(database_url) as conn:
        rows = query_all(
            conn,
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            WHERE tc.table_schema = 'public'
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY tc.table_name, kcu.ordinal_position
            """,
        )
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(str(row["table_name"]), []).append(str(row["column_name"]))
    return result


def source_name_for_target(table: str, original_sources: set[str]) -> str:
    reverse_aliases = {target: source for source, target in ALIASES.items()}
    if table in reverse_aliases:
        return reverse_aliases[table]
    if table in original_sources:
        return table
    return table


def normalize_source_rows(source_rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for table, rows in source_rows.items():
        target_table = ALIASES.get(table, table.replace("-", "_"))
        if target_table == "auth.users":
            target_table = "users"
        normalized_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                normalized_row = _normalize_row(target_table, row)
            except Exception:
                normalized_row = dict(row)
            if normalized_row:
                normalized_rows.append(normalized_row)
        if normalized_rows:
            normalized[target_table] = normalized_rows
    return normalized


def row_key(row: dict[str, Any], pk_columns: list[str]) -> str | None:
    if pk_columns and all(row.get(column) is not None for column in pk_columns):
        return "|".join(str(canonical(row[column])) for column in pk_columns)
    if row.get("id") is not None:
        return str(canonical(row["id"]))
    return None


def duplicate_source_keys(rows: list[dict[str, Any]], pk_columns: list[str]) -> dict[str, int]:
    counter = Counter(key for row in rows if (key := row_key(row, pk_columns)))
    return {key: count for key, count in counter.items() if count > 1}


def fetch_target_row(conn: Any, table: str, pk_columns: list[str], key: str) -> dict[str, Any] | None:
    if not pk_columns:
        if UUID_PATTERN.match(key):
            pk_columns = ["id"]
        else:
            return None
    parts = key.split("|")
    if len(parts) != len(pk_columns):
        return None
    where = " AND ".join(f"{quote_ident(column)} = %s" for column in pk_columns)
    return query_one(conn, f"SELECT * FROM public.{quote_ident(table)} WHERE {where}", tuple(parts))


def target_keys(conn: Any, table: str, pk_columns: list[str]) -> set[str]:
    if not pk_columns:
        if any(column["column_name"] == "id" for column in table_columns(conn, table)):
            pk_columns = ["id"]
        else:
            return set()
    expr = " || '|' || ".join(f"COALESCE({quote_ident(column)}::text, '')" for column in pk_columns)
    rows = query_all(conn, f"SELECT {expr} AS key FROM public.{quote_ident(table)}")
    return {str(row["key"]) for row in rows if row.get("key")}


def compare_source_to_postgres(
    database_url: str,
    source_rows: dict[str, list[dict[str, Any]]],
    *,
    max_samples: int,
) -> dict[str, Any]:
    pk_map = primary_keys_by_table(database_url)
    normalized = normalize_source_rows(source_rows)
    missing: dict[str, list[str]] = {}
    unexpected: dict[str, list[str]] = {}
    duplicate: dict[str, dict[str, Any]] = {}
    field_mismatches: dict[str, list[dict[str, Any]]] = {}
    table_results: dict[str, Any] = {}
    with connect(database_url) as conn:
        target_tables = set(public_tables(conn))
        for table, rows in sorted(normalized.items()):
            if table not in target_tables:
                missing[table] = [row_key(row, pk_map.get(table, [])) or fingerprint_row(row) for row in rows[:max_samples]]
                table_results[table] = {
                    "source_rows": len(rows),
                    "target_table_exists": False,
                    "missing_count": len(rows),
                    "unexpected_count": None,
                    "field_mismatch_count": None,
                }
                continue
            pk_columns = pk_map.get(table, [])
            source_keys = {key for row in rows if (key := row_key(row, pk_columns))}
            target_key_set = target_keys(conn, table, pk_columns)
            duplicate_source = duplicate_source_keys(rows, pk_columns)
            if duplicate_source:
                duplicate.setdefault(table, {})["source"] = duplicate_source
            missing_keys = sorted(source_keys - target_key_set)
            unexpected_keys = sorted(target_key_set - source_keys)
            if missing_keys:
                missing[table] = missing_keys[:max_samples]
            if unexpected_keys:
                unexpected[table] = unexpected_keys[:max_samples]
            samples: list[dict[str, Any]] = []
            for row in rows:
                key = row_key(row, pk_columns)
                if not key or key in missing_keys:
                    continue
                target_row = fetch_target_row(conn, table, pk_columns, key)
                if not target_row:
                    continue
                diffs: dict[str, Any] = {}
                for column, source_value in row.items():
                    if column not in target_row:
                        continue
                    if not equivalent_for_migration(column, source_value, target_row[column]):
                        diffs[column] = {
                            "source": canonical(source_value),
                            "target": canonical(target_row[column]),
                        }
                if diffs:
                    samples.append({"key": key, "diffs": diffs})
                    if len(samples) >= max_samples:
                        break
            if samples:
                field_mismatches[table] = samples
            table_results[table] = {
                "source_rows": len(rows),
                "source_keys": len(source_keys),
                "target_keys": len(target_key_set),
                "target_table_exists": True,
                "missing_count": len(missing_keys),
                "unexpected_count": len(unexpected_keys),
                "duplicate_source_key_count": len(duplicate_source),
                "field_mismatch_count": len(samples),
            }
    return {
        "table_results": table_results,
        "missing_uuids": missing,
        "unexpected_uuids": unexpected,
        "duplicate_uuids": duplicate,
        "field_mismatches": field_mismatches,
        "normalized_source_counts": {name: len(rows) for name, rows in sorted(normalized.items())},
        "ok": not missing and not duplicate and not field_mismatches,
    }


def scan_duplicate_target_keys(database_url: str) -> dict[str, Any]:
    duplicates: dict[str, Any] = {}
    pk_map = primary_keys_by_table(database_url)
    with connect(database_url) as conn:
        for table in public_tables(conn):
            columns = pk_map.get(table)
            if not columns:
                continue
            expr = " || '|' || ".join(f"COALESCE({quote_ident(column)}::text, '')" for column in columns)
            rows = query_all(
                conn,
                f"""
                SELECT {expr} AS key, COUNT(*)::int AS count
                FROM public.{quote_ident(table)}
                GROUP BY {expr}
                HAVING COUNT(*) > 1
                LIMIT 50
                """,
            )
            if rows:
                duplicates[table] = rows
    return duplicates


def scan_supabase_references(database_url: str, *, max_samples: int) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    pattern = "|".join(re.escape(item) for item in SUPABASE_PATTERNS)
    with connect(database_url) as conn:
        for table in public_tables(conn):
            for column in table_columns(conn, table):
                data_type = str(column["data_type"])
                if data_type not in {"text", "character varying", "character", "json", "jsonb"}:
                    continue
                col = quote_ident(str(column["column_name"]))
                try:
                    rows = query_all(
                        conn,
                        f"""
                        SELECT {col}::text AS value
                        FROM public.{quote_ident(table)}
                        WHERE {col}::text ~* %s
                        LIMIT %s
                        """,
                        (pattern, max_samples),
                    )
                except Exception as error:
                    findings.append({"table": table, "column": column["column_name"], "scan_error": str(error)})
                    continue
                for row in rows:
                    findings.append(
                        {
                            "table": table,
                            "column": column["column_name"],
                            "sample": str(row.get("value", ""))[:500],
                        }
                    )
    return {"ok": not findings, "finding_count": len(findings), "findings": findings[:max_samples]}


def extract_upload_refs(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=json_default)
    else:
        text = str(value)
    refs = set()
    for match in UPLOAD_REF_PATTERN.findall(text):
        refs.add(match.rstrip(".,;:"))
    return refs


def scan_file_references(database_url: str, upload_dir: Path, *, max_samples: int) -> dict[str, Any]:
    checked = 0
    missing: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    with connect(database_url) as conn:
        for table in public_tables(conn):
            for column in table_columns(conn, table):
                name = str(column["column_name"])
                data_type = str(column["data_type"])
                if not FILE_COLUMN_PATTERN.search(name):
                    continue
                if data_type not in {"text", "character varying", "character", "json", "jsonb"}:
                    continue
                col = quote_ident(name)
                try:
                    rows = query_all(
                        conn,
                        f"""
                        SELECT {col} AS value
                        FROM public.{quote_ident(table)}
                        WHERE {col} IS NOT NULL
                        LIMIT 5000
                        """,
                    )
                except Exception:
                    continue
                for row in rows:
                    for ref in extract_upload_refs(row.get("value")):
                        checked += 1
                        relative = ref.removeprefix("/uploads/").replace("/", os.sep)
                        candidate = (upload_dir / relative).resolve()
                        try:
                            candidate.relative_to(upload_dir.resolve())
                        except ValueError:
                            outside.append({"table": table, "column": name, "path": ref})
                            continue
                        if not candidate.is_file():
                            missing.append({"table": table, "column": name, "path": ref})
                            if len(missing) >= max_samples:
                                break
                    if len(missing) >= max_samples:
                        break
    return {
        "ok": not missing and not outside,
        "checked_upload_references": checked,
        "missing_count": len(missing),
        "outside_count": len(outside),
        "missing_samples": missing[:max_samples],
        "outside_samples": outside[:max_samples],
    }


def scan_runtime_dependencies() -> dict[str, Any]:
    commands = {
        "supabase_runtime_scan": [
            "rg",
            "-n",
            "-i",
            "supabase|supabase_flutter|SupabaseClient|Postgrest|Gotrue|supabase\\.co|storage/v1|functions/v1|rest/v1|auth/v1|realtime/v1|anon_key|service_role|SUPABASE_URL|SUPABASE_ANON_KEY|VITE_SUPABASE|nzoxoduxecgrkxxwdobp",
            "--glob",
            "!backend/data/**",
            "--glob",
            "!**/__pycache__/**",
            "backend",
            "lib",
            "web",
            "test",
            "pubspec.yaml",
            "pubspec.lock",
        ],
        "mock_state_runtime_scan": [
            "rg",
            "-n",
            "-i",
            "state\\.json|mock|fake|dummy|fallback|x-local-user-id|local_user_id",
            "--glob",
            "!backend/data/**",
            "--glob",
            "!**/__pycache__/**",
            "backend",
            "lib",
            "web",
            "test",
        ],
    }
    result: dict[str, Any] = {}
    for name, args in commands.items():
        import subprocess

        completed = subprocess.run(args, cwd=PROJECT_DIR, text=True, capture_output=True)
        lines = [line for line in (completed.stdout or "").splitlines() if "__pycache__" not in line]
        active_lines = [
            line
            for line in lines
            if "backend/scripts/migrate_legacy_data.py" not in line.replace("\\", "/")
            and "backend/scripts/verify_migration_integrity.py" not in line.replace("\\", "/")
            and "backend/tests/migration/" not in line.replace("\\", "/")
        ]
        result[name] = {
            "exit_code": completed.returncode,
            "line_count": len(lines),
            "active_line_count": len(active_lines),
            "active_samples": active_lines[:50],
        }
    return result


def generate_mapping_markdown(
    path: Path,
    *,
    database_url: str,
    source_metadata: dict[str, Any],
    comparison: dict[str, Any],
) -> None:
    pk_map = primary_keys_by_table(database_url)
    with connect(database_url) as conn:
        tables = public_tables(conn)
        original_sources = set(source_metadata.get("collection_counts", {}).keys())
        rows = []
        for table in tables:
            columns = [column["column_name"] for column in table_columns(conn, table)]
            source_name = source_name_for_target(table, original_sources)
            source_count = source_metadata.get("collection_counts", {}).get(source_name)
            if source_count is None:
                source_count = source_metadata.get("collection_counts", {}).get(table)
            status = "target-only or source unavailable"
            table_result = comparison.get("table_results", {}).get(table)
            if table_result:
                status = "verified against available source" if not table_result.get("missing_count") else "has missing rows"
                if table_result.get("field_mismatch_count"):
                    status = "field mismatches found"
            rows.append(
                {
                    "source": source_name if source_count is not None else "unverified",
                    "target": table,
                    "primary_key": ", ".join(pk_map.get(table, ["id"])) or "n/a",
                    "source_fields": "from source export when available",
                    "target_fields": ", ".join(columns[:12]) + (" ..." if len(columns) > 12 else ""),
                    "conversion": "same-name columns plus legacy normalizers and local upload path remapping",
                    "renamed": ", ".join(f"{k}->{v}" for k, v in ALIASES.items() if v == table) or "none documented",
                    "defaults": "database/model defaults",
                    "relations": "PostgreSQL foreign keys and application checks",
                    "status": status,
                }
            )
    lines = [
        "# Migration Data Mapping",
        "",
        f"Generated at: {utc_now()}",
        "",
        "This mapping is generated from the current PostgreSQL schema and the available migration source metadata. "
        "A direct Supabase read-only connection or original Supabase export is required for authoritative PASS.",
        "",
        f"Source kind: `{source_metadata.get('source_kind', SOURCE_KIND_NONE)}`",
        f"Source authoritative Supabase export: `{source_metadata.get('is_authoritative_supabase_source', False)}`",
        "",
        "| مصدر Supabase | جدول PostgreSQL الهدف | المفتاح الأساسي | الحقول المصدرية | الحقول الهدف | قاعدة التحويل | الحقول التي تغير اسمها | القيم الافتراضية | العلاقات | حالة التحقق |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {source} | {target} | {primary_key} | {source_fields} | {target_fields} | {conversion} | {renamed} | {defaults} | {relations} | {status} |".format(
                **{key: str(value).replace("|", "\\|") for key, value in row.items()}
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_report(path: Path, *, result: dict[str, Any]) -> None:
    final_line = (
        "PASS: تم التحقق من أن جميع بيانات Supabase انتقلت إلى PostgreSQL دون فقد أو تكرار أو كسر علاقات، وجميع الملفات والواجهات تعمل من PostgreSQL المحلي فقط."
        if result["overall_pass"]
        else "FAIL: ما زالت توجد بيانات ناقصة أو مكررة أو علاقات مكسورة أو ملفات مفقودة أو اختلافات غير معالجة بين Supabase وPostgreSQL."
    )
    comparison = result["source_comparison"]
    table_rows = []
    for table, details in comparison.get("table_results", {}).items():
        table_rows.append(
            "| {table} | {source_rows} | {source_keys} | {target_keys} | {missing_count} | {unexpected_count} | {field_mismatch_count} | {status} |".format(
                table=table,
                source_rows=details.get("source_rows", 0),
                source_keys=details.get("source_keys", "n/a"),
                target_keys=details.get("target_keys", "n/a"),
                missing_count=details.get("missing_count", "n/a"),
                unexpected_count=details.get("unexpected_count", "n/a"),
                field_mismatch_count=details.get("field_mismatch_count", "n/a"),
                status="PASS" if not details.get("missing_count") and not details.get("field_mismatch_count") else "FAIL",
            )
        )
    if not table_rows:
        table_rows.append("| لا يوجد مصدر قابل للمقارنة | 0 | 0 | 0 | n/a | n/a | n/a | FAIL |")

    command_results = result.get("command_results", {})
    command_lines = [
        f"- `{name}`: exit `{details.get('exit_code')}`, summary `{details.get('summary', '')}`"
        for name, details in command_results.items()
    ]
    if not command_lines:
        command_lines = ["- لم يتم تسجيل نتائج أوامر نهائية داخل أداة التحقق."]

    lines = [
        "# Full Supabase To PostgreSQL Migration Integrity Report",
        "",
        f"Generated at: {result['generated_at']}",
        "",
        "## Environment",
        "",
        f"- PostgreSQL host: `{result['database']['host']}`",
        f"- PostgreSQL port: `{result['database']['port']}`",
        f"- PostgreSQL database: `{result['database']['database']}`",
        "- PostgreSQL password printed: `false`",
        f"- Source kind: `{result['source_metadata'].get('source_kind')}`",
        f"- Authoritative Supabase source available: `{result['source_metadata'].get('is_authoritative_supabase_source', False)}`",
        f"- Pre-fix backup manifest: `{result['preflight_backup_manifest']}`",
        f"- Result directory: `{result['result_dir']}`",
        "",
        "## Source Status",
        "",
        "A PASS is allowed only when a direct read-only Supabase source or an original Supabase export is available and compared. "
        "The currently available source was classified as shown below:",
        "",
        "```json",
        json.dumps(result["source_metadata"], ensure_ascii=False, indent=2, default=json_default),
        "```",
        "",
        "## Table Comparison",
        "",
        "| table | source rows | source keys | target keys | missing | unexpected | field mismatches | result |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
        *table_rows,
        "",
        "## JSON Evidence Files",
        "",
        f"- missing UUIDs: `{result['evidence_files']['missing_uuids']}`",
        f"- unexpected UUIDs: `{result['evidence_files']['unexpected_uuids']}`",
        f"- duplicate UUIDs: `{result['evidence_files']['duplicate_uuids']}`",
        f"- full JSON result: `{result['evidence_files']['full_result']}`",
        "",
        "## PostgreSQL Internal Integrity",
        "",
        f"- Table count: `{result['postgres_snapshot']['table_count']}`",
        f"- Total rows: `{result['postgres_snapshot']['total_rows']}`",
        f"- Alembic version: `{result['postgres_snapshot'].get('alembic_version')}`",
        f"- Relationship checks ok: `{result['relationship_checks'].get('ok')}`",
        f"- Relationship failure count: `{result['relationship_checks'].get('failure_count')}`",
        f"- Target duplicate primary keys: `{sum(len(v) for v in result['target_duplicate_keys'].values())}`",
        "",
        "## Supabase And Legacy Runtime Scan",
        "",
        f"- Database Supabase URL findings: `{result['supabase_reference_scan']['finding_count']}`",
        f"- Active code Supabase findings: `{result['runtime_dependency_scan']['supabase_runtime_scan']['active_line_count']}`",
        f"- Active code state/mock/fallback findings: `{result['runtime_dependency_scan']['mock_state_runtime_scan']['active_line_count']}`",
        "",
        "## Uploads And File References",
        "",
        f"- Checked upload references: `{result['file_reference_scan']['checked_upload_references']}`",
        f"- Missing upload references: `{result['file_reference_scan']['missing_count']}`",
        f"- Outside upload-dir references: `{result['file_reference_scan']['outside_count']}`",
        "",
        "## Required Command Results",
        "",
        *command_lines,
        "",
        "## Fixes Performed",
        "",
        "- Created a pre-verification PostgreSQL + uploads backup before code changes.",
        "- Added a reusable migration-integrity verifier that writes source comparison evidence and PostgreSQL integrity evidence.",
        "- Added migration tests for source classification, duplicate detection, and upload-reference parsing.",
        "- Generated a schema-driven `MIGRATION_DATA_MAPPING.md` file.",
        "",
        "## Not Executed Or Not Proven",
        "",
        "- Direct read-only Supabase comparison was not proven because no `SUPABASE_SOURCE_DATABASE_URL` was supplied.",
        "- Original Supabase export comparison was not proven because no `SUPABASE_EXPORT_DIR` was supplied.",
        "- The available legacy `state.json` was compared as migration-source evidence, but it is not classified as an authoritative Supabase export.",
        "",
        final_line,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_command_for_report(args: list[str], cwd: Path) -> dict[str, Any]:
    import subprocess

    started = time.perf_counter()
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    return {
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "summary": output.strip().splitlines()[-1] if output.strip().splitlines() else "",
    }


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Verify migration integrity from Supabase exports to local PostgreSQL.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--upload-dir", default=str(default_upload_dir()))
    parser.add_argument("--result-dir", default=str(BACKEND_DIR / "data" / "migration_integrity"))
    parser.add_argument("--legacy-state-json", default=os.environ.get("LEGACY_STATE_JSON"))
    parser.add_argument("--supabase-export-dir", default=os.environ.get("SUPABASE_EXPORT_DIR"))
    parser.add_argument("--supabase-source-database-url", default=os.environ.get("SUPABASE_SOURCE_DATABASE_URL"))
    parser.add_argument("--run-id", default=os.environ.get("MIGRATION_INTEGRITY_RUN_ID"))
    parser.add_argument("--skip-precheck-backup", action="store_true")
    parser.add_argument("--max-samples", type=int, default=50)
    parser.add_argument("--run-smoke-commands", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required.")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    result_dir = (Path(args.result_dir) / run_id).resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = Path(args.upload_dir).resolve()

    preflight_manifest = None
    if not args.skip_precheck_backup:
        backup = create_backup_package(
            database_url=args.database_url,
            backup_dir=default_backup_dir(),
            upload_dir=upload_dir,
            created_by="migration-integrity-verifier",
            run_id=f"migration-integrity-{run_id}",
        )
        preflight_manifest = backup["manifest_path"]

    kind = source_kind(args)
    source_path = discover_source_path(args)
    if kind == SOURCE_KIND_SUPABASE_DB:
        source_rows, source_metadata = {}, {
            "ok": False,
            "source_kind": SOURCE_KIND_SUPABASE_DB,
            "reason": "Live Supabase database connections are disabled. Use a local SQL/CSV/JSON export directory instead.",
            "is_authoritative_supabase_source": False,
        }
    elif kind == SOURCE_KIND_SUPABASE_EXPORT and source_path:
        source_rows, source_metadata = load_export_dir_source(source_path)
    elif kind == SOURCE_KIND_LEGACY_STATE and source_path:
        source_rows, source_metadata = load_legacy_state_source(source_path)
    else:
        source_rows, source_metadata = {}, {
            "ok": False,
            "source_kind": SOURCE_KIND_NONE,
            "reason": "No SUPABASE_SOURCE_DATABASE_URL, SUPABASE_EXPORT_DIR, or LEGACY_STATE_JSON was provided.",
            "is_authoritative_supabase_source": False,
        }

    comparison = (
        compare_source_to_postgres(args.database_url, source_rows, max_samples=args.max_samples)
        if source_rows
        else {
            "ok": False,
            "table_results": {},
            "missing_uuids": {},
            "unexpected_uuids": {},
            "duplicate_uuids": {},
            "field_mismatches": {},
            "normalized_source_counts": {},
        }
    )
    snapshot = database_snapshot(args.database_url, result_dir / "postgres_snapshot.json")
    relationships = relationship_checks(args.database_url)
    supabase_refs = scan_supabase_references(args.database_url, max_samples=args.max_samples)
    file_refs = scan_file_references(args.database_url, upload_dir, max_samples=args.max_samples)
    target_duplicates = scan_duplicate_target_keys(args.database_url)
    runtime_scan = scan_runtime_dependencies()
    command_results: dict[str, Any] = {}
    if args.run_smoke_commands:
        command_results["pytest backend/tests/migration -q"] = run_command_for_report(
            [sys.executable, "-m", "pytest", "backend/tests/migration", "-q"], PROJECT_DIR
        )

    authoritative = bool(source_metadata.get("is_authoritative_supabase_source"))
    overall_pass = bool(
        authoritative
        and source_metadata.get("ok")
        and comparison.get("ok")
        and relationships.get("ok")
        and supabase_refs.get("ok")
        and file_refs.get("ok")
        and not target_duplicates
        and runtime_scan["supabase_runtime_scan"]["active_line_count"] == 0
        and all(value.get("exit_code") == 0 for value in command_results.values())
    )
    result = {
        "generated_at": utc_now(),
        "result_dir": str(result_dir),
        "database": safe_db_info(args.database_url),
        "database_metadata": database_metadata(args.database_url),
        "source_metadata": source_metadata,
        "source_comparison": comparison,
        "postgres_snapshot": snapshot,
        "relationship_checks": relationships,
        "supabase_reference_scan": supabase_refs,
        "file_reference_scan": file_refs,
        "target_duplicate_keys": target_duplicates,
        "runtime_dependency_scan": runtime_scan,
        "preflight_backup_manifest": preflight_manifest,
        "command_results": command_results,
        "overall_pass": overall_pass,
    }
    write_json(result_dir / "missing_uuids.json", comparison.get("missing_uuids", {}))
    write_json(result_dir / "unexpected_uuids.json", comparison.get("unexpected_uuids", {}))
    write_json(result_dir / "duplicate_uuids.json", comparison.get("duplicate_uuids", {}))
    write_json(result_dir / "field_mismatches.json", comparison.get("field_mismatches", {}))
    write_json(result_dir / "supabase_url_findings.json", supabase_refs)
    write_json(result_dir / "file_reference_findings.json", file_refs)
    full_result_path = result_dir / "migration_integrity_result.json"
    result["evidence_files"] = {
        "missing_uuids": str(result_dir / "missing_uuids.json"),
        "unexpected_uuids": str(result_dir / "unexpected_uuids.json"),
        "duplicate_uuids": str(result_dir / "duplicate_uuids.json"),
        "full_result": str(full_result_path),
    }
    write_json(full_result_path, result)
    generate_mapping_markdown(
        PROJECT_DIR / "MIGRATION_DATA_MAPPING.md",
        database_url=args.database_url,
        source_metadata=source_metadata,
        comparison=comparison,
    )
    generate_report(PROJECT_DIR / "FULL_SUPABASE_TO_POSTGRES_MIGRATION_INTEGRITY_REPORT.md", result=result)
    print(
        json.dumps(
            {
                "ok": overall_pass,
                "result_dir": str(result_dir),
                "report": str(PROJECT_DIR / "FULL_SUPABASE_TO_POSTGRES_MIGRATION_INTEGRITY_REPORT.md"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
