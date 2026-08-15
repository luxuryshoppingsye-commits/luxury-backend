from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.sqltypes import Boolean, DateTime, Integer, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.app.config import BACKEND_DIR, PROJECT_DIR, get_settings
from backend.app.models import Base, MODEL_BY_TABLE


LEGACY_TABLE = "luxury_collections"
ALIASES = {
    "roles": "user_roles",
    "cart": "user_cart",
    "courier_locations": "courier_location_updates",
    "theme-settings": "theme_settings",
    "site-settings": "site_settings",
    "pages": "static_pages",
}
SKIP_COLLECTIONS = {"meta", "admin_records", "catalog_products", "managed_products", "products"}
IMPORT_ORDER = [
    "users", "profiles", "user_roles", "categories", "brands", "products",
    "product_variants", "orders", "order_items", "order_payments",
    "order_status_history", "notifications", "admin_notifications",
]
ASSET_REMAPS = {
    "/local-assets/product-images/": "/uploads/products/",
    "/local-assets/migrated-assets/": "/uploads/products/",
    "/local-assets/product-variant-images/": "/uploads/product-variants/",
    "/local-assets/avatars/": "/uploads/avatars/",
    "/local-assets/payment-receipts/": "/uploads/payment-receipts/",
    "/local-assets/partner-documents/": "/uploads/partner-documents/",
    "/local-assets/site-assets/": "/uploads/site-assets/",
    "/local-assets/support/": "/uploads/support/",
    "/local-assets/placeholders/": "/uploads/placeholders/",
}
MISSING_ASSETS: set[str] = set()
REMAPPED_ASSETS: set[str] = set()

INTERNAL_VISIBLE_TEXT_PATTERNS = (
    re.compile(r"\bCODEX\b|CODEX_", re.IGNORECASE),
    re.compile(r"\bE2E\b|E2E_", re.IGNORECASE),
    re.compile(r"\bTEST\b|TEST_", re.IGNORECASE),
    re.compile(r"\bMOCK\b|\bDUMMY\b|\bSAMPLE\b|\bFIXTURE\b|RUN_ID", re.IGNORECASE),
    re.compile(r"^Imported product\b", re.IGNORECASE),
    re.compile(r"^Unknown product\b|^Unknown item\b", re.IGNORECASE),
    re.compile(r"^Product\s+[0-9a-f_-]{5,}$", re.IGNORECASE),
    re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    ),
)


def _safe_public_display_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return not any(pattern.search(text) for pattern in INTERNAL_VISIBLE_TEXT_PATTERNS)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def _read_state_file() -> dict[str, Any]:
    candidates: list[Path] = []
    legacy_path = os.getenv("LEGACY_STATE_JSON", "").strip()
    if legacy_path:
        candidates.append(Path(legacy_path))
    candidates.append(BACKEND_DIR / "data" / "state.json")

    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _read_legacy_database(sync_url: str) -> dict[str, Any]:
    raw_url = sync_url.replace("postgresql+psycopg://", "postgresql://", 1)
    result: dict[str, Any] = {}
    with psycopg.connect(raw_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", (f"public.{LEGACY_TABLE}",))
            if cursor.fetchone()[0] is None:
                return result
            cursor.execute(f'SELECT name, payload FROM "{LEGACY_TABLE}"')
            for name, payload in cursor.fetchall():
                result[str(name)] = payload
    return result


def _merge_sources(file_state: dict[str, Any], database_state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(file_state)
    for key, value in database_state.items():
        if isinstance(value, list):
            existing = merged.get(key)
            if isinstance(existing, list):
                by_id = {str(row.get("id")): row for row in existing if isinstance(row, dict) and row.get("id")}
                no_id = [row for row in existing if not isinstance(row, dict) or not row.get("id")]
                for row in value:
                    if isinstance(row, dict) and row.get("id"):
                        by_id[str(row["id"])] = row
                    else:
                        no_id.append(row)
                merged[key] = [*by_id.values(), *no_id]
            else:
                merged[key] = value
        else:
            merged[key] = value
    return merged


def _flatten_admin_records(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    records = state.get("admin_records")
    if not isinstance(records, dict):
        return result
    for raw_name, rows in records.items():
        name = ALIASES.get(raw_name, raw_name.replace("-", "_"))
        if name not in MODEL_BY_TABLE or not isinstance(rows, list):
            continue
        normalized: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            normalized.append({**raw, **data})
        result[name] = normalized
    return result


def _dedupe_products(state: dict[str, Any]) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for source in ("catalog_products", "products", "managed_products"):
        rows = state.get(source)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            key = str(row["id"])
            products[key] = {**products.get(key, {}), **row}
    return list(products.values())


def _collections(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result = _flatten_admin_records(state)
    for raw_name, rows in state.items():
        if raw_name in SKIP_COLLECTIONS or not isinstance(rows, list):
            continue
        name = ALIASES.get(raw_name, raw_name)
        if name not in MODEL_BY_TABLE:
            continue
        result[name] = [dict(row) for row in rows if isinstance(row, dict)]
    result["products"] = _dedupe_products(state)
    if "categories" in result:
        pending = list(result["categories"])
        ordered_categories: list[dict[str, Any]] = []
        known: set[str] = set()
        while pending:
            ready = [row for row in pending if not row.get("parent_id") or str(row.get("parent_id")) in known]
            if not ready:
                ready = pending
            for row in ready:
                ordered_categories.append(row)
                if row.get("id"):
                    known.add(str(row["id"]))
                pending.remove(row)
        result["categories"] = ordered_categories
    _ensure_dependencies(result)
    for name, rows in list(result.items()):
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("id"):
                key = f"id:{row['id']}"
            elif name == "user_roles":
                key = f"role:{row.get('user_id')}:{row.get('role')}"
            else:
                key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            unique[key] = row
        result[name] = list(unique.values())
    return result


def _ensure_dependencies(collections: dict[str, list[dict[str, Any]]]) -> None:
    users = collections.setdefault("users", [])
    known_users = {str(row.get("id")) for row in users if row.get("id")}
    referenced_users: set[str] = set()
    for table, rows in collections.items():
        for row in rows:
            for field in ("user_id", "recipient_id", "sender_id", "created_by", "reviewed_by"):
                value = _uuid(row.get(field))
                if value:
                    referenced_users.add(str(value))
    for user_id in sorted(referenced_users - known_users):
        users.append({
            "id": user_id,
            "email": f"migrated-{user_id}@invalid.local",
            "password_hash": "!legacy-account-requires-password-reset!",
            "password_must_reset": True,
            "is_active": False,
            "extra_data": {"migration_note": "Placeholder for an orphaned legacy reference"},
        })
        collections.setdefault("profiles", []).append({
            "id": user_id,
            "user_id": user_id,
            "email": f"migrated-{user_id}@invalid.local",
            "full_name": "حساب قديم مؤرشف",
        })

    products = collections.setdefault("products", [])
    known_products = {str(row.get("id")) for row in products if row.get("id")}
    referenced_products: set[str] = set()
    for table in ("product_variants", "order_items", "inventory", "wishlist", "user_cart"):
        for row in collections.get(table, []):
            value = _uuid(row.get("product_id") or row.get("productId"))
            if value:
                referenced_products.add(str(value))
    for product_id in sorted(referenced_products - known_products):
        products.append({
            "id": product_id,
            "name": f"منتج قديم مؤرشف {product_id[:8]}",
            "price": 0,
            "stock_quantity": 0,
            "is_active": False,
            "approval_status": "archived",
            "extra_data": {"migration_note": "Placeholder for orphaned variants or order items"},
        })

    orders = collections.setdefault("orders", [])
    known_orders = {str(row.get("id")) for row in orders if row.get("id")}
    referenced_orders: set[str] = set()
    for table in ("order_items", "order_status_history", "order_payments"):
        for row in collections.get(table, []):
            value = _uuid(row.get("order_id"))
            if value:
                referenced_orders.add(str(value))
    fallback_user = next((str(row.get("id")) for row in users if row.get("id")), None)
    if fallback_user:
        for order_id in sorted(referenced_orders - known_orders):
            orders.append({
                "id": order_id,
                "order_number": f"MIG-{order_id[:12].upper()}",
                "user_id": fallback_user,
                "status": "archived",
                "total": 0,
                "payment_status": "unknown",
                "extra_data": {"migration_note": "Placeholder for orphaned legacy order items"},
            })


def _uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _numeric(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _normalize_asset_reference(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_asset_reference(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_asset_reference(item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/")
    target = None
    for old_prefix, new_prefix in ASSET_REMAPS.items():
        if normalized.startswith(old_prefix):
            target = f"{new_prefix}{normalized.removeprefix(old_prefix)}"
            break
    if target is None and normalized.startswith(("http://", "https://")):
        filename = Path(urlparse(normalized).path).name
        if filename:
            matches = list(get_settings().resolved_upload_dir.rglob(filename))
            if matches:
                target = f"/uploads/{matches[0].relative_to(get_settings().resolved_upload_dir).as_posix()}"
    if target is None:
        return value
    REMAPPED_ASSETS.add(value)
    relative = target.removeprefix("/uploads/")
    if not (get_settings().resolved_upload_dir / relative).is_file():
        MISSING_ASSETS.add(value)
    return target


def _coerce(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, UUID):
        return _uuid(value)
    if isinstance(column.type, DateTime):
        return _datetime(value)
    if isinstance(column.type, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "active", "enabled"}
        return bool(value)
    if isinstance(column.type, Integer):
        try:
            return int(float(str(value)))
        except ValueError:
            return 0
    if isinstance(column.type, Numeric):
        return _numeric(value)
    if isinstance(column.type, JSONB):
        return value
    return str(value) if value is not None else None


def _normalize_row(table_name: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    table = Base.metadata.tables[table_name]
    source = _normalize_asset_reference(dict(raw))
    if table_name == "products":
        source["image_url"] = source.get("image_url") or source.get("imageUrl")
        if source.get("images") is None:
            source["images"] = []
        if source.get("tags") is None:
            source["tags"] = []
        source["name"] = source.get("name") or source.get("title")
        if not _safe_public_display_text(source.get("name")):
            return None
    elif table_name == "profiles":
        source["user_id"] = source.get("user_id") or source.get("id")
        source["id"] = source.get("id") or source.get("user_id")
    elif table_name == "user_roles":
        if not source.get("user_id") or not source.get("role"):
            return None
    elif table_name == "user_cart":
        source["product_id"] = source.get("product_id") or source.get("productId")
        source["variant_id"] = source.get("variant_id") or source.get("variantId")
    elif table_name == "courier_location_updates":
        source["courier_id"] = source.get("courier_id") or source.get("courier_user_id")

    if "id" in table.c and (not source.get("id") or _uuid(source.get("id")) is None):
        original_id = source.get("id")
        stable = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
        source["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"luxury:{table_name}:{stable}"))
        if original_id:
            source["legacy_id"] = str(original_id)

    known: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in source.items():
        if key in table.c and key != "extra_data":
            coerced = _coerce(table.c[key], value)
            if coerced is not None:
                known[key] = coerced
        elif key not in {"data", "imageUrl"}:
            extra[key] = value
    if "extra_data" in table.c:
        previous = source.get("extra_data")
        if isinstance(previous, dict):
            extra = {**previous, **extra}
        known["extra_data"] = extra
    return known


def _import_rows(connection, table_name: str, rows: list[dict[str, Any]]) -> tuple[int, int]:
    table = Base.metadata.tables[table_name]
    inserted = 0
    skipped = 0
    for raw in rows:
        row = _normalize_row(table_name, raw)
        if not row:
            skipped += 1
            continue
        primary_keys = [column.name for column in table.primary_key.columns]
        if not primary_keys or any(row.get(key) is None for key in primary_keys):
            skipped += 1
            continue
        statement = insert(table).values(**row)
        updates = {key: statement.excluded[key] for key in row if key not in primary_keys and key in table.c}
        if updates:
            statement = statement.on_conflict_do_update(index_elements=primary_keys, set_=updates)
        else:
            statement = statement.on_conflict_do_nothing(index_elements=primary_keys)
        try:
            with connection.begin_nested():
                connection.execute(statement)
            inserted += 1
        except Exception as error:
            skipped += 1
            print(f"SKIP {table_name} {row.get('id')}: {error}", file=sys.stderr)
    return inserted, skipped


def _copy_uploads(upload_root: Path) -> tuple[int, list[str]]:
    mappings = {
        "avatars": "avatars",
        "product-images": "products",
        "migrated-assets": "products",
        "product-variant-images": "product-variants",
        "payment-receipts": "payment-receipts",
        "partner-documents": "partner-documents",
        "site-assets": "site-assets",
        "support": "support",
    }
    copied = 0
    failures: list[str] = []
    upload_root.mkdir(parents=True, exist_ok=True)
    for source_name, target_name in mappings.items():
        source = upload_root / source_name
        target = upload_root / target_name
        target.mkdir(parents=True, exist_ok=True)
        if not source.exists() or source.resolve() == target.resolve():
            continue
        for file in source.rglob("*"):
            if not file.is_file():
                continue
            destination = target / file.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not destination.exists() or hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(file.read_bytes()).digest():
                    shutil.copy2(file, destination)
                copied += 1
            except OSError as error:
                failures.append(f"{file}: {error}")
    return copied, failures


def _counts(connection, names: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in names:
        table = Base.metadata.tables[name]
        result[name] = int(connection.execute(select(func.count()).select_from(table)).scalar_one())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotently migrate legacy Luxury Shopping data")
    parser.add_argument("--report", default=str(BACKEND_DIR / "data" / "migration-report.json"))
    args = parser.parse_args()
    settings = get_settings()
    sync_url = _sync_url(settings.database_url)
    state = _merge_sources(_read_state_file(), _read_legacy_database(sync_url))
    collections = _collections(state)
    ordered = [name for name in IMPORT_ORDER if name in collections]
    ordered.extend(sorted(name for name in collections if name not in ordered))
    engine = create_engine(sync_url)
    results: dict[str, Any] = {}
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    with engine.begin() as connection:
        for name in ordered:
            table = Base.metadata.tables[name]
            before[name] = int(connection.execute(select(func.count()).select_from(table)).scalar_one())
            inserted, skipped = _import_rows(connection, name, collections[name])
            after[name] = int(connection.execute(select(func.count()).select_from(table)).scalar_one())
            results[name] = {"source": len(collections[name]), "processed": inserted, "skipped": skipped}
    copied, file_failures = _copy_uploads(settings.resolved_upload_dir)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_collections": {name: len(rows) for name, rows in collections.items()},
        "before": before,
        "after": after,
        "results": results,
        "uploads_copied_or_verified": copied,
        "upload_failures": file_failures,
        "asset_references_remapped": len(REMAPPED_ASSETS),
        "missing_asset_references": sorted(MISSING_ASSETS),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if file_failures or any(value["skipped"] for value in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
