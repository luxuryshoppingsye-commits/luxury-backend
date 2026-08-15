from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings


DISPLAY_COLUMNS = {
    "name",
    "name_en",
    "display_name",
    "title",
    "label",
    "slug",
    "sku",
    "short_code",
    "code",
    "subject",
    "description",
    "description_ar",
    "description_en",
    "body",
    "message",
    "store_name",
    "owner_name",
    "recipient_name",
    "order_number",
    "meta_title",
    "meta_description",
    "promotional_title",
    "image_url",
    "logo_url",
    "url",
    "path",
}

BANNED_RE = re.compile(
    r"(\bCODEX\b|CODEX_|\bE2E\b|E2E_|\bTEST\b|TEST_|\bMOCK\b|MOCK_|"
    r"\bFAKE\b|\bDUMMY\b|\bSAMPLE\b|\bFIXTURE\b|RUN_ID|AI_GENERATED|"
    r"AUTOMATED_TEST|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)

GENERATED_ID_RE = re.compile(r"\b(?:[0-9a-fA-F]{10,}|[0-9]{12,})\b")


def sync_dsn() -> str:
    return str(get_settings().database_url).replace("postgresql+asyncpg://", "postgresql://", 1)


def _is_suspicious(column: str, value: str) -> bool:
    if BANNED_RE.search(value):
        return True
    if column in {"name", "name_en", "display_name", "title", "label", "slug", "sku", "code", "subject"}:
        return bool(GENERATED_ID_RE.search(value))
    return False


def scan_visible_fields(limit_per_column: int) -> dict[str, Any]:
    settings = get_settings()
    findings: list[dict[str, Any]] = []
    with psycopg.connect(sync_dsn()) as conn:
        database_name = conn.execute("select current_database()").fetchone()[0]
        columns = conn.execute(
            """
            select table_schema, table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
              and column_name = any(%s)
              and data_type in ('character varying', 'text', 'character', 'jsonb', 'json')
            order by table_name, ordinal_position
            """,
            (sorted(DISPLAY_COLUMNS),),
        ).fetchall()

        table_columns: dict[str, set[str]] = {}
        for _, table, column, _ in conn.execute(
            """
            select table_schema, table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
            """
        ):
            table_columns.setdefault(table, set()).add(column)

        for schema, table, column, _ in columns:
            value_expr = f'"{column}"::text'
            id_expr = "id::text" if "id" in table_columns.get(table, set()) else "null::text"
            created_expr = (
                "created_at::text" if "created_at" in table_columns.get(table, set()) else "null::text"
            )
            sql = (
                f'select {id_expr} as id, {created_expr} as created_at, {value_expr} as value '
                f'from "{schema}"."{table}" '
                f'where "{column}" is not null limit %s'
            )
            for row_id, created_at, value in conn.execute(sql, (limit_per_column,)).fetchall():
                text_value = str(value or "")
                if _is_suspicious(column, text_value):
                    findings.append(
                        {
                            "table": table,
                            "id": row_id,
                            "column": column,
                            "value": text_value[:500],
                            "created_at": created_at,
                        }
                    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_env": settings.app_env,
        "database_name": database_name,
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check user-visible PostgreSQL fields for leaked test data.")
    parser.add_argument("--output", default="")
    parser.add_argument("--limit-per-column", type=int, default=100000)
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args()

    report = scan_visible_fields(args.limit_per_column)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("app_env", "database_name", "finding_count")}, ensure_ascii=False))

    protected_env = str(report["app_env"]).lower() in {"staging", "production"}
    if report["finding_count"] and (protected_env or args.fail_on_findings):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
