from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings


ARTIFACT_DIR = PROJECT_ROOT / "data_hygiene_artifacts" / "final_verification"
DATABASE_ARTIFACT_DIR = PROJECT_ROOT / "data_hygiene_artifacts" / "database"

REQUESTED_TABLES = [
    "users",
    "profiles",
    "user_roles",
    "products",
    "product_variants",
    "product_images",
    "categories",
    "brands",
    "merchants",
    "partner_profiles",
    "partner_storefronts",
    "orders",
    "order_items",
    "payments",
    "order_payments",
    "payment_receipts",
    "refunds",
    "inventory",
    "inventory_movements",
    "wallets",
    "partner_wallets",
    "wallet_transactions",
    "merchant_commissions",
    "partner_payments",
    "marketer_commissions",
    "notifications",
    "support_tickets",
    "ticket_messages",
    "refresh_tokens",
    "uploaded_files",
    "audit_logs",
]

MARKER_RE = re.compile(
    r"(CODEX|E2E|TEST_|_TEST|MOCK|DUMMY|SAMPLE|FIXTURE|PERF-|perf-|"
    r"@example\.test|@example\.com|AUTOMATED|RUN_ID)",
    re.IGNORECASE,
)


def sync_dsn() -> str:
    return str(get_settings().database_url).replace("postgresql+asyncpg://", "postgresql://", 1)


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def table_columns(conn: psycopg.Connection) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        select table_name, column_name
        from information_schema.columns
        where table_schema = 'public'
        """
    ).fetchall()
    columns: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        columns[row["table_name"]].add(row["column_name"])
    return columns


def aggregate_deleted_counts() -> dict[str, int]:
    deleted: dict[str, int] = defaultdict(int)
    for name in [
        "cleanup_executed_20260721T000937Z.json",
        "cleanup_imported_products_20260721T001116Z.json",
        "cleanup_remaining_internal_products_20260721T001150Z.json",
    ]:
        data = load_json(DATABASE_ARTIFACT_DIR / name)
        for table, count in (data.get("deleted_counts") or {}).items():
            deleted[table] += int(count or 0)
    return dict(sorted(deleted.items()))


def count_tables(conn: psycopg.Connection, columns: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    deleted = aggregate_deleted_counts()
    counts: dict[str, dict[str, Any]] = {}
    for table in REQUESTED_TABLES:
        if table not in columns:
            counts[table] = {
                "exists": False,
                "before_cleanup_estimate": deleted.get(table, 0),
                "deleted_by_cleanup_artifacts": deleted.get(table, 0),
                "after_cleanup": None,
            }
            continue
        after = conn.execute(f'select count(*) as count from "{table}"').fetchone()["count"]
        active = None
        if "deleted_at" in columns[table]:
            active = conn.execute(
                f'select count(*) as count from "{table}" where deleted_at is null'
            ).fetchone()["count"]
        counts[table] = {
            "exists": True,
            "before_cleanup_estimate": int(after) + deleted.get(table, 0),
            "deleted_by_cleanup_artifacts": deleted.get(table, 0),
            "after_cleanup": int(after),
            "active_after_cleanup": None if active is None else int(active),
        }
    return counts


def cleanup_evidence(columns: dict[str, set[str]]) -> list[dict[str, Any]]:
    before = load_json(DATABASE_ARTIFACT_DIR / "test_data_findings_before_summary.json")
    executed = load_json(DATABASE_ARTIFACT_DIR / "cleanup_executed_20260721T000937Z.json")
    deleted = aggregate_deleted_counts()
    before_distinct = before.get("distinct_rows_by_table") or {}
    samples = executed.get("samples") or {}
    rows: list[dict[str, Any]] = []
    for table, count in deleted.items():
        sample_rows = samples.get(table, [])
        sample_text = json.dumps(sample_rows[:10], ensure_ascii=False)
        has_marker = bool(MARKER_RE.search(sample_text))
        has_visible_findings = int(before_distinct.get(table) or 0) > 0
        relationship_table = table in {
            "order_items",
            "order_payments",
            "payment_receipts",
            "payments",
            "refunds",
            "inventory",
            "inventory_movements",
            "notifications",
            "refresh_tokens",
            "ticket_messages",
            "partner_wallets",
            "partner_payments",
            "marketer_commissions",
        }
        if has_marker or has_visible_findings:
            evidence = "direct visible/internal marker evidence"
            risk = "LOW"
            verification = "SUPPORTED"
        elif relationship_table:
            evidence = "relationship cleanup artifact only; parent test-root evidence must be reviewed"
            risk = "MEDIUM"
            verification = "RISK_REVIEW_REQUIRED"
        else:
            evidence = "cleanup artifact count exists but sample evidence is insufficient"
            risk = "HIGH"
            verification = "RISK_REVIEW_REQUIRED"
        rows.append(
            {
                "table": table,
                "deleted_rows": count,
                "selection_predicate": "artifact-driven cleanup batch; exact SQL predicate not present in report",
                "test_evidence": evidence,
                "possible_real_data_risk": risk,
                "backup_reference": "data_hygiene_artifacts/database/pre_cleanup_luxury_20260721T000344Z.dump",
                "verification_result": verification,
                "table_exists_now": table in columns,
            }
        )
    return rows


def fk_orphans(conn: psycopg.Connection) -> list[dict[str, Any]]:
    constraints = conn.execute(
        """
        select
          con.conname,
          src.relname as source_table,
          tgt.relname as target_table,
          array_agg(src_att.attname order by ord.ordinality) as source_columns,
          array_agg(tgt_att.attname order by ord.ordinality) as target_columns
        from pg_constraint con
        join pg_class src on src.oid = con.conrelid
        join pg_namespace nsp on nsp.oid = src.relnamespace
        join pg_class tgt on tgt.oid = con.confrelid
        join unnest(con.conkey) with ordinality as ord(attnum, ordinality) on true
        join pg_attribute src_att on src_att.attrelid = con.conrelid and src_att.attnum = ord.attnum
        join pg_attribute tgt_att on tgt_att.attrelid = con.confrelid and tgt_att.attnum = con.confkey[ord.ordinality]
        where con.contype = 'f' and nsp.nspname = 'public'
        group by con.conname, src.relname, tgt.relname
        order by src.relname, con.conname
        """
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for row in constraints:
        source_cols = list(row["source_columns"])
        target_cols = list(row["target_columns"])
        null_clause = " or ".join(f's."{column}" is not null' for column in source_cols)
        join_clause = " and ".join(
            f's."{source}" = t."{target}"'
            for source, target in zip(source_cols, target_cols, strict=False)
        )
        target_null = " and ".join(f't."{column}" is null' for column in target_cols)
        sql = (
            f'select count(*) as count from "{row["source_table"]}" s '
            f'left join "{row["target_table"]}" t on {join_clause} '
            f'where ({null_clause}) and {target_null}'
        )
        count = int(conn.execute(sql).fetchone()["count"])
        if count:
            findings.append(
                {
                    "constraint": row["conname"],
                    "source_table": row["source_table"],
                    "source_columns": source_cols,
                    "target_table": row["target_table"],
                    "target_columns": target_cols,
                    "orphan_count": count,
                }
            )
    return findings


def custom_integrity_checks(conn: psycopg.Connection, columns: dict[str, set[str]]) -> list[dict[str, Any]]:
    checks: list[tuple[str, str]] = []
    if {"users", "profiles"}.issubset(columns):
        checks.append(
            (
                "users_without_profile",
                "select count(*) as count from users u left join profiles p on p.user_id = u.id where p.id is null",
            )
        )
        checks.append(
            (
                "profiles_without_user",
                "select count(*) as count from profiles p left join users u on u.id = p.user_id where u.id is null",
            )
        )
    if {"users", "user_roles"}.issubset(columns):
        checks.append(
            (
                "roles_without_user",
                "select count(*) as count from user_roles r left join users u on u.id = r.user_id where u.id is null",
            )
        )
    if {"orders", "users"}.issubset(columns):
        checks.append(
            (
                "orders_without_customer",
                "select count(*) as count from orders o left join users u on u.id = o.user_id where u.id is null",
            )
        )
    if {"order_items", "orders"}.issubset(columns):
        checks.append(
            (
                "order_items_without_order",
                "select count(*) as count from order_items i left join orders o on o.id = i.order_id where o.id is null",
            )
        )
    if {"product_variants", "products"}.issubset(columns):
        checks.append(
            (
                "product_variants_without_product",
                "select count(*) as count from product_variants v left join products p on p.id = v.product_id where p.id is null",
            )
        )
    if {"inventory", "products"}.issubset(columns):
        checks.append(
            (
                "inventory_missing_product_reference",
                "select count(*) as count from inventory i left join products p on p.id = i.product_id where i.product_id is not null and p.id is null",
            )
        )
    if {"inventory", "product_variants"}.issubset(columns):
        checks.append(
            (
                "inventory_missing_variant_reference",
                "select count(*) as count from inventory i left join product_variants v on v.id = i.variant_id where i.variant_id is not null and v.id is null",
            )
        )
    if "inventory" in columns:
        checks.append(("inventory_without_product_or_variant", "select count(*) as count from inventory where product_id is null and variant_id is null"))
        checks.append(("negative_inventory_quantity", "select count(*) as count from inventory where quantity < 0"))
    if "inventory_movements" in columns:
        checks.append(("negative_inventory_movement_quantity", "select count(*) as count from inventory_movements where quantity < 0"))
    if {"refunds", "payments"}.issubset(columns) and "payment_id" in columns["refunds"]:
        checks.append(
            (
                "refunds_without_payment",
                "select count(*) as count from refunds r left join payments p on p.id = r.payment_id where r.payment_id is not null and p.id is null",
            )
        )
    if {"refunds", "orders"}.issubset(columns):
        checks.append(
            (
                "refunds_without_order",
                "select count(*) as count from refunds r left join orders o on o.id = r.order_id where r.order_id is not null and o.id is null",
            )
        )
    if {"marketer_commissions", "users"}.issubset(columns):
        checks.append(
            (
                "marketer_commissions_without_user",
                "select count(*) as count from marketer_commissions c left join users u on u.id = c.user_id where c.user_id is not null and u.id is null",
            )
        )
    if {"marketer_commissions", "orders"}.issubset(columns):
        checks.append(
            (
                "marketer_commissions_without_order",
                "select count(*) as count from marketer_commissions c left join orders o on o.id = c.order_id where c.order_id is not null and o.id is null",
            )
        )
    if {"notifications", "users"}.issubset(columns):
        checks.append(
            (
                "notifications_without_user",
                "select count(*) as count from notifications n left join users u on u.id = n.user_id where n.user_id is not null and u.id is null",
            )
        )
    if {"ticket_messages", "support_tickets"}.issubset(columns):
        checks.append(
            (
                "ticket_messages_without_ticket",
                "select count(*) as count from ticket_messages m left join support_tickets t on t.id = m.ticket_id where t.id is null",
            )
        )
    results = []
    for name, sql in checks:
        count = int(conn.execute(sql).fetchone()["count"])
        results.append({"check": name, "count": count, "status": "PASS" if count == 0 else "FAIL"})
    return results


def financial_checks(conn: psycopg.Connection, columns: dict[str, set[str]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    sums: dict[str, Any] = {}
    for table in ["payments", "order_payments", "payment_receipts", "refunds", "partner_payments", "partner_settlements", "marketer_commissions"]:
        if table in columns and "amount" in columns[table]:
            row = conn.execute(
                f'select count(*) as count, coalesce(sum(amount), 0) as amount from "{table}"'
            ).fetchone()
            sums[table] = {"count": int(row["count"]), "amount": json_value(row["amount"])}
    if {"payments", "orders"}.issubset(columns):
        count = int(
            conn.execute(
                "select count(*) as count from payments p left join orders o on o.id = p.order_id where p.order_id is not null and o.id is null"
            ).fetchone()["count"]
        )
        checks.append({"check": "payments_without_order", "count": count, "status": "PASS" if count == 0 else "FAIL"})
    if {"order_payments", "orders"}.issubset(columns):
        count = int(
            conn.execute(
                "select count(*) as count from order_payments p left join orders o on o.id = p.order_id where p.order_id is not null and o.id is null"
            ).fetchone()["count"]
        )
        checks.append({"check": "order_payments_without_order", "count": count, "status": "PASS" if count == 0 else "FAIL"})
    if {"refunds", "orders"}.issubset(columns):
        count = int(
            conn.execute(
                "select count(*) as count from refunds r left join orders o on o.id = r.order_id where r.order_id is not null and o.id is null"
            ).fetchone()["count"]
        )
        checks.append({"check": "refunds_without_order", "count": count, "status": "PASS" if count == 0 else "FAIL"})
    return {"totals_after_cleanup": sums, "checks": checks}


def file_checks(conn: psycopg.Connection, columns: dict[str, set[str]]) -> dict[str, Any]:
    settings = get_settings()
    upload_root = settings.resolved_upload_dir
    db_refs: set[str] = set()
    missing_refs: list[dict[str, Any]] = []
    for table, table_cols in columns.items():
        for column in table_cols.intersection({"image_url", "logo_url", "path", "url"}):
            rows = conn.execute(
                f'select "{column}"::text as value from "{table}" where "{column}" is not null limit 100000'
            ).fetchall()
            for row in rows:
                value = str(row["value"] or "")
                if not value.startswith("/uploads/"):
                    continue
                relative = value.removeprefix("/uploads/").replace("/", "\\")
                db_refs.add(relative.replace("\\", "/"))
                if not (upload_root / relative).is_file():
                    missing_refs.append({"table": table, "column": column, "value": value})
    files = []
    if upload_root.exists():
        files = [path for path in upload_root.rglob("*") if path.is_file()]
    file_rel = {path.relative_to(upload_root).as_posix() for path in files}
    orphan_files = sorted(file_rel - db_refs)
    return {
        "upload_root": str(upload_root),
        "db_file_references": len(db_refs),
        "physical_files": len(file_rel),
        "missing_file_references": missing_refs[:200],
        "missing_file_reference_count": len(missing_refs),
        "orphan_files": orphan_files[:200],
        "orphan_file_count": len(orphan_files),
    }


def run() -> dict[str, Any]:
    settings = get_settings()
    with psycopg.connect(sync_dsn(), row_factory=dict_row) as conn:
        columns = table_columns(conn)
        database_name = conn.execute("select current_database() as name").fetchone()["name"]
        encoding = conn.execute("show server_encoding").fetchone()["server_encoding"]
        constraints_not_valid = conn.execute(
            """
            select conname, contype, conrelid::regclass::text as table_name
            from pg_constraint
            where not convalidated
            order by table_name, conname
            """
        ).fetchall()
        duplicate_constraints = conn.execute(
            """
            select conname, conrelid::regclass::text as table_name, count(*) as count
            from pg_constraint
            group by conname, conrelid
            having count(*) > 1
            """
        ).fetchall()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "app_env": settings.app_env,
                "database_name": database_name,
                "configured_database_name": settings.database_name,
                "allow_test_fixtures": settings.allow_test_fixtures,
                "fixtures_enabled": settings.fixtures_enabled,
                "storage_environment": settings.storage_environment,
                "server_encoding": encoding,
            },
            "counts_by_table": count_tables(conn, columns),
            "cleanup_evidence": cleanup_evidence(columns),
            "database_integrity": {
                "foreign_key_orphans": fk_orphans(conn),
                "custom_checks": custom_integrity_checks(conn, columns),
                "constraints_not_valid": [dict(row) for row in constraints_not_valid],
                "duplicate_constraints": [dict(row) for row in duplicate_constraints],
            },
            "financial_integrity": financial_checks(conn, columns),
            "file_integrity": file_checks(conn, columns),
        }
        return report


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Data Hygiene Final Verification",
        "",
        f"Generated at: `{report['generated_at']}`",
        "",
        "## Environment",
        "",
        "| Key | Value |",
        "|---|---|",
    ]
    for key, value in report["environment"].items():
        lines.append(f"| `{key}` | `{value}` |")
    lines.extend(["", "## Exact Counts", "", "| Table | Exists | Before cleanup estimate | Deleted | After cleanup | Active after cleanup |", "|---|---:|---:|---:|---:|---:|"])
    for table, row in report["counts_by_table"].items():
        lines.append(
            f"| `{table}` | {row['exists']} | {row['before_cleanup_estimate']} | "
            f"{row['deleted_by_cleanup_artifacts']} | {row['after_cleanup']} | {row.get('active_after_cleanup')} |"
        )
    lines.extend(["", "## Deletion Evidence Review", "", "| Table | Deleted Rows | Predicate | Evidence | Risk | Backup | Result |", "|---|---:|---|---|---|---|---|"])
    for row in report["cleanup_evidence"]:
        lines.append(
            f"| `{row['table']}` | {row['deleted_rows']} | {row['selection_predicate']} | "
            f"{row['test_evidence']} | {row['possible_real_data_risk']} | `{row['backup_reference']}` | {row['verification_result']} |"
        )
    db = report["database_integrity"]
    lines.extend(["", "## Database Integrity", ""])
    lines.append(f"- Foreign-key orphan findings: `{len(db['foreign_key_orphans'])}`")
    lines.append(f"- Not-valid constraints: `{len(db['constraints_not_valid'])}`")
    lines.append(f"- Duplicate constraint definitions: `{len(db['duplicate_constraints'])}`")
    lines.extend(["", "| Custom check | Count | Status |", "|---|---:|---|"])
    for row in db["custom_checks"]:
        lines.append(f"| `{row['check']}` | {row['count']} | {row['status']} |")
    fin = report["financial_integrity"]
    lines.extend(["", "## Financial Integrity", "", "| Check | Count | Status |", "|---|---:|---|"])
    for row in fin["checks"]:
        lines.append(f"| `{row['check']}` | {row['count']} | {row['status']} |")
    lines.extend(["", "### Financial Totals After Cleanup", "", "| Table | Count | Amount |", "|---|---:|---:|"])
    for table, row in fin["totals_after_cleanup"].items():
        lines.append(f"| `{table}` | {row['count']} | {row['amount']} |")
    files = report["file_integrity"]
    lines.extend(["", "## File Integrity", ""])
    lines.append(f"- DB upload references: `{files['db_file_references']}`")
    lines.append(f"- Physical upload files: `{files['physical_files']}`")
    lines.append(f"- Missing file references: `{files['missing_file_reference_count']}`")
    lines.append(f"- Orphan physical files: `{files['orphan_file_count']}`")
    risk_count = sum(1 for row in report["cleanup_evidence"] if row["verification_result"] != "SUPPORTED")
    fail_checks = [
        row for row in db["custom_checks"] + fin["checks"] if row["status"] != "PASS"
    ]
    pass_ready = (
        report["environment"]["app_env"] == "test"
        and report["environment"]["database_name"].endswith("test")
        and not db["foreign_key_orphans"]
        and not db["constraints_not_valid"]
        and not fail_checks
        and files["missing_file_reference_count"] == 0
        and files["orphan_file_count"] == 0
        and risk_count == 0
    )
    lines.extend(["", "## Decision", ""])
    if pass_ready:
        lines.append("PASS: Data hygiene final verification passed within the checked database scope.")
    else:
        lines.append(
            "FAIL: Final Data Hygiene PASS is not yet defensible because one or more "
            "environment, deletion-evidence, integrity, financial, or file checks require review."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Final read-only verification for test-data cleanup.")
    parser.add_argument("--output-dir", default=str(ARTIFACT_DIR))
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = run()
    json_path = output_dir / f"data_hygiene_final_verification_{stamp}.json"
    markdown_path = output_dir / f"data_hygiene_final_verification_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=json_value), encoding="utf-8")
    write_markdown(report, markdown_path)
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
