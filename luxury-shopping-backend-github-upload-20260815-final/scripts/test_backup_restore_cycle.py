from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backup_postgres import (
    BACKEND_DIR,
    PROJECT_DIR,
    assert_safe_restore_target,
    compare_upload_manifests,
    connect,
    create_backup_package,
    database_name,
    database_snapshot,
    default_backup_dir,
    default_upload_dir,
    drop_restore_database,
    load_env_files,
    query_one,
    relationship_checks,
    replace_database,
    restore_backup_package,
    safe_db_info,
    sha256_file,
    to_async_url,
    to_sync_url,
    upload_manifest,
    verify_backup_manifest,
)


INTERNAL_PREFIX = "LSH_BACKUP_RESTORE"
SMALL_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(base_url: str, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=3)
            if response.status_code == 200:
                return
            last_error = f"status {response.status_code}: {response.text[:200]}"
        except Exception as error:
            last_error = str(error)
        time.sleep(0.8)
    raise RuntimeError(f"FastAPI did not become healthy at {base_url}: {last_error}")


def start_fastapi(*, database_url: str, upload_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = {**os.environ}
    env["DATABASE_URL"] = to_sync_url(database_url)
    env["UPLOAD_DIR"] = str(upload_dir)
    env["API_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["APP_PUBLIC_URL"] = f"http://127.0.0.1:{port}"
    env["WS_BASE_URL"] = f"ws://127.0.0.1:{port}"
    env["PYTHONPATH"] = str(PROJECT_DIR)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=str(PROJECT_DIR),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_health(f"http://127.0.0.1:{port}")
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def request_json(method: str, url: str, *, token: str | None = None, json_body: dict[str, Any] | None = None, expected: set[int] | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=30) as client:
        response = client.request(method, url, json=json_body, headers=request_headers)
    if expected and response.status_code not in expected:
        raise RuntimeError(f"Unexpected API status {response.status_code} for {method} {url}: {response.text[:500]}")
    if response.status_code == 204:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, response.text


def login(base_url: str, email: str, password: str) -> dict[str, Any]:
    _, payload = request_json("POST", f"{base_url}/auth/login", json_body={"email": email, "password": password}, expected={200})
    if not payload.get("access_token"):
        raise RuntimeError("Login response did not contain an access token.")
    return payload


def restored_upload_url(base_url: str, stored_url: str) -> str:
    parsed = urlsplit(str(stored_url))
    if parsed.hostname in {"127.0.0.1", "localhost", "testserver"} and parsed.path.startswith("/uploads/"):
        base = urlsplit(base_url)
        return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))
    if str(stored_url).startswith("/uploads/"):
        base = urlsplit(base_url)
        return urlunsplit((base.scheme, base.netloc, str(stored_url), "", ""))
    return stored_url


def require_isolated_test_source(database_url: str) -> None:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    allow_fixtures = os.environ.get("ALLOW_TEST_FIXTURES", "").strip().lower() in {"1", "true", "yes", "on"}
    db_name = database_name(database_url).lower()
    if app_env != "test" or not allow_fixtures or "test" not in db_name:
        raise SystemExit(
            "backup restore verification data requires APP_ENV=test, "
            "ALLOW_TEST_FIXTURES=true, and a source database name containing 'test'"
        )


def create_seed_data(base_url: str, run_id: str, admin_password: str) -> dict[str, Any]:
    admin = login(base_url, "riiso@msn.com", admin_password)
    admin_token = admin["access_token"]
    customer_email = f"lsh.backup.restore.{run_id.lower()}@example.com"
    customer_password = f"Restore{run_id[-6:]}!512"
    _, customer = request_json(
        "POST",
        f"{base_url}/auth/register-customer",
        json_body={
            "email": customer_email,
            "password": customer_password,
            "fullName": "عميل رفاهية",
            "phone": "+967771234567",
            "city": "Sanaa",
        },
        expected={201},
    )
    customer_token = customer["access_token"]
    _, avatar = request_json(
        "POST",
        f"{base_url}/me/avatar",
        token=customer_token,
        json_body={"fileName": "profile-image.png", "dataBase64": SMALL_PNG_BASE64},
        expected={200},
    )
    _, category = request_json(
        "POST",
        f"{base_url}/admin/sections/categories/records",
        token=admin_token,
        json_body={
            "name": "إلكترونيات",
            "slug": f"backup-restore-electronics-{run_id.lower()}",
            "is_active": True,
            "sort_order": 991,
        },
        expected={200},
    )
    _, brand = request_json(
        "POST",
        f"{base_url}/admin/sections/brands/records",
        token=admin_token,
        json_body={
            "name": "علامة فاخرة",
            "slug": f"backup-restore-brand-{run_id.lower()}",
            "is_active": True,
        },
        expected={200},
    )
    _, product_image = request_json(
        "POST",
        f"{base_url}/manage/product-image",
        token=admin_token,
        json_body={"fileName": "product-image.png", "dataBase64": SMALL_PNG_BASE64},
        expected={200},
    )
    merchant_email = f"lsh.backup.restore.merchant.{run_id.lower()}@example.com"
    merchant_password = f"Merchant{run_id[-6:]}!512"
    _, merchant = request_json(
        "POST",
        f"{base_url}/auth/register-merchant",
        json_body={
            "email": merchant_email,
            "password": merchant_password,
            "ownerName": "راشد عبدالله",
            "storeName": "متجر التقنية الحديثة",
            "phone": "+967731234567",
            "city": "Sanaa",
            "description": "طلب انضمام متجر تقني موثوق.",
            "logoUrl": product_image["imageUrl"],
        },
        expected={201},
    )
    _, applications = request_json("GET", f"{base_url}/admin/partner-applications", token=admin_token, expected={200})
    application = next(row for row in applications if row.get("email") == merchant_email)
    _, reviewed = request_json(
        "POST",
        f"{base_url}/admin/partner-applications/{application['id']}/review",
        token=admin_token,
        json_body={"status": "approved"},
        expected={200},
    )
    merchant_login = login(base_url, merchant_email, merchant_password)
    merchant_token = merchant_login["access_token"]
    _, product = request_json(
        "POST",
        f"{base_url}/manage/products",
        token=admin_token,
        json_body={
            "name": "سماعة لاسلكية احترافية",
            "sku": f"CBR-{run_id}",
            "description": "منتج مختار بعناية ضمن كتالوج رفاهية التسوق.",
            "price": "1500.00",
            "originalPrice": "1700.00",
            "stockQuantity": 12,
            "categoryId": category["id"],
            "brandId": brand["id"],
            "imageUrl": product_image["imageUrl"],
            "isActive": True,
            "approvalStatus": "approved",
        },
        expected={201},
    )
    _, partner_product = request_json(
        "POST",
        f"{base_url}/manage/products",
        token=merchant_token,
        json_body={
            "name": "ساعة ذكية رياضية",
            "sku": f"CBR-PARTNER-{run_id}",
            "description": "ساعة ذكية مناسبة للاستخدام اليومي والرياضي.",
            "price": "1900.00",
            "stockQuantity": 4,
            "categoryId": category["id"],
            "brandId": brand["id"],
            "imageUrl": product_image["imageUrl"],
            "isActive": True,
        },
        expected={201},
    )
    _, variant = request_json(
        "POST",
        f"{base_url}/manage/products/{product['id']}/variants",
        token=admin_token,
        json_body={"sku": f"CBR-{run_id}-V1", "size": "M", "color": "Gold", "price": "1500.00", "stockQuantity": 5, "isActive": True},
        expected={200, 201},
    )
    _, cart = request_json(
        "POST",
        f"{base_url}/cart",
        token=customer_token,
        json_body={"productId": product["id"], "variantId": variant["id"], "quantity": 1},
        expected={200, 201},
    )
    _, wishlist = request_json(
        "POST",
        f"{base_url}/wishlist",
        token=customer_token,
        json_body={"productId": product["id"]},
        expected={200, 201},
    )
    _, order = request_json(
        "POST",
        f"{base_url}/orders/checkout",
        token=customer_token,
        json_body={
            "paymentMethod": "bank_transfer",
            "shippingAddress": {"city": "Sanaa", "address": "شارع الزبيري"},
            "notes": "يرجى التواصل قبل التسليم.",
            "idempotencyKey": f"backup-restore-{run_id}",
        },
        expected={201},
        headers={"Idempotency-Key": f"backup-restore-{run_id}"},
    )
    _, receipt = request_json(
        "POST",
        f"{base_url}/orders/{order['id']}/payment-receipt",
        token=customer_token,
        json_body={"fileName": "payment-receipt.png", "dataBase64": SMALL_PNG_BASE64, "amount": order["total"]},
        expected={201},
    )
    _, ticket = request_json(
        "POST",
        f"{base_url}/support/tickets",
        token=customer_token,
        json_body={"subject": "استفسار عن الطلب", "description": "أحتاج إلى مساعدة بخصوص حالة الطلب."},
        expected={201},
    )
    return {
        "run_id": run_id,
        "customer_email": customer_email,
        "customer_password_redacted": True,
        "merchant_email": merchant_email,
        "merchant_password_redacted": True,
        "customer_id": customer["user"]["id"],
        "merchant_id": merchant["user"]["id"],
        "partner_application_id": application["id"],
        "category_id": category["id"],
        "brand_id": brand["id"],
        "product_id": product["id"],
        "partner_product_id": partner_product["id"],
        "variant_id": variant["id"],
        "cart_id": cart["id"],
        "wishlist_id": wishlist["id"],
        "order_id": order["id"],
        "receipt_id": receipt["id"],
        "ticket_id": ticket["id"],
        "avatar_url": avatar["avatarUrl"],
        "product_image_url": product_image["imageUrl"],
        "partner_review_status": reviewed["application"]["status"],
    }


def verify_seed_ids(database_url: str, seed: dict[str, Any]) -> dict[str, Any]:
    checks = {}
    with connect(database_url) as conn:
        for name, table, item_id in (
            ("customer", "users", seed["customer_id"]),
            ("merchant", "users", seed["merchant_id"]),
            ("partner_application", "partner_applications", seed["partner_application_id"]),
            ("category", "categories", seed["category_id"]),
            ("brand", "brands", seed["brand_id"]),
            ("product", "products", seed["product_id"]),
            ("partner_product", "products", seed["partner_product_id"]),
            ("variant", "product_variants", seed["variant_id"]),
            ("order", "orders", seed["order_id"]),
            ("receipt", "payment_receipts", seed["receipt_id"]),
            ("ticket", "support_tickets", seed["ticket_id"]),
        ):
            row = query_one(conn, f"SELECT COUNT(*) AS count FROM {table} WHERE id = %s", (item_id,))
            checks[name] = int(row["count"]) if row else 0
    return {"checks": checks, "ok": all(value == 1 for value in checks.values())}


def exercise_restored_application(base_url: str, restore_url: str, source_url: str, seed: dict[str, Any], admin_password: str, run_id: str) -> dict[str, Any]:
    admin = login(base_url, "riiso@msn.com", admin_password)
    customer = login(base_url, seed["customer_email"], seed.get("_customer_password", ""))
    merchant = login(base_url, seed["merchant_email"], seed.get("_merchant_password", ""))
    admin_token = admin["access_token"]
    customer_token = customer["access_token"]
    merchant_token = merchant["access_token"]
    _, me = request_json("GET", f"{base_url}/me", token=customer_token, expected={200})
    _, merchant_me = request_json("GET", f"{base_url}/me", token=merchant_token, expected={200})
    _, storefront = request_json("GET", f"{base_url}/partner/storefront", token=merchant_token, expected={200})
    _, orders = request_json("GET", f"{base_url}/orders", token=customer_token, expected={200})
    _, product_detail = request_json("GET", f"{base_url}/products/{seed['product_id']}", expected={200})
    restored_image_url = restored_upload_url(base_url, product_detail["imageUrl"])
    image_response = httpx.get(restored_image_url, timeout=15)
    if image_response.status_code != 200:
        raise RuntimeError(f"Restored product image returned {image_response.status_code}")
    _, restore_product = request_json(
        "POST",
        f"{base_url}/manage/products",
        token=admin_token,
        json_body={
            "name": "حقيبة جلدية أنيقة",
            "sku": f"CBR-RESTORE-{run_id}",
            "price": "2200.00",
            "stockQuantity": 3,
            "isActive": True,
            "approvalStatus": "approved",
        },
        expected={201},
    )
    _, updated = request_json(
        "PATCH",
        f"{base_url}/manage/products/{restore_product['id']}",
        token=admin_token,
        json_body={"description": "وصف محدث لمنتج تجاري داخل قاعدة الاستعادة."},
        expected={200},
    )
    request_json("DELETE", f"{base_url}/manage/products/{restore_product['id']}", token=admin_token, expected={200})
    source_count = product_count_by_sku(source_url, f"CBR-RESTORE-{run_id}")
    restore_count = product_count_by_sku(restore_url, f"CBR-RESTORE-{run_id}", include_deleted=True)
    return {
        "admin_login": True,
        "customer_login": me["user"]["email"] == seed["customer_email"],
        "merchant_login": merchant_me["user"]["email"] == seed["merchant_email"],
        "merchant_partner_role": "partner" in merchant_me.get("roles", []),
        "partner_storefront_ok": storefront.get("partner_id") == seed["merchant_id"] or storefront.get("user_id") == seed["merchant_id"],
        "orders_visible": any(row["id"] == seed["order_id"] for row in orders),
        "product_detail_ok": product_detail["id"] == seed["product_id"],
        "product_image_original_url": product_detail["imageUrl"],
        "product_image_restored_url": restored_image_url,
        "product_image_uses_restored_api": restored_image_url.startswith(base_url),
        "product_image_http_status": image_response.status_code,
        "restore_crud_product_id": restore_product["id"],
        "restore_crud_update_ok": updated.get("description") == "وصف محدث لمنتج تجاري داخل قاعدة الاستعادة.",
        "restore_only_source_count": source_count,
        "restore_only_restore_count": restore_count,
        "ok": source_count == 0 and restore_count == 1,
    }


def product_count_by_sku(database_url: str, sku: str, *, include_deleted: bool = False) -> int:
    with connect(database_url) as conn:
        if include_deleted:
            row = query_one(conn, "SELECT COUNT(*) AS count FROM products WHERE sku = %s", (sku,))
        else:
            row = query_one(conn, "SELECT COUNT(*) AS count FROM products WHERE sku = %s AND deleted_at IS NULL", (sku,))
        return int(row["count"]) if row else 0


def check_corrupted_backup_detection(manifest_path: Path) -> dict[str, Any]:
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump_path = Path(original["postgres_dump"]["path"])
    corrupted = manifest_path.parent / f"{dump_path.stem}.truncated.dump"
    shutil.copy2(dump_path, corrupted)
    size = corrupted.stat().st_size
    with corrupted.open("r+b") as handle:
        handle.truncate(max(1, size // 2))
    corrupted_manifest = dict(original)
    corrupted_manifest["postgres_dump"] = dict(original["postgres_dump"])
    corrupted_manifest["postgres_dump"]["path"] = str(corrupted)
    corrupted_manifest["postgres_dump"]["name"] = corrupted.name
    corrupted_manifest_path = manifest_path.parent / "corrupted_backup_manifest.json"
    corrupted_manifest_path.write_text(json.dumps(corrupted_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    from backup_postgres import verify_backup_manifest

    result = verify_backup_manifest(corrupted_manifest_path)
    return {"ok": result["ok"] is False, "verification": result}


def check_missing_uploads_detection(manifest_path: Path) -> dict[str, Any]:
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing_manifest = dict(original)
    missing_manifest["uploads_archive"] = dict(original["uploads_archive"])
    missing_manifest["uploads_archive"]["path"] = str(manifest_path.parent / "missing_uploads.zip")
    missing_manifest_path = manifest_path.parent / "missing_uploads_manifest.json"
    missing_manifest_path.write_text(json.dumps(missing_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    from backup_postgres import verify_backup_manifest

    result = verify_backup_manifest(missing_manifest_path)
    return {"ok": result["ok"] is False, "verification": result}


def run_alembic_check(database_url: str, run_dir: Path) -> dict[str, Any]:
    env = {**os.environ, "DATABASE_URL": to_sync_url(database_url), "PYTHONPATH": str(PROJECT_DIR)}
    results: dict[str, Any] = {}
    for name, args in {
        "current": [sys.executable, "-m", "alembic", "current"],
        "check": [sys.executable, "-m", "alembic", "check"],
    }.items():
        completed = subprocess.run(args, cwd=str(BACKEND_DIR), env=env, text=True, capture_output=True)
        output = {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        (run_dir / f"alembic_{name}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        results[name] = {"returncode": completed.returncode, "ok": completed.returncode == 0}
    results["ok"] = all(item["ok"] for item in results.values() if isinstance(item, dict))
    return results


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Run a full PostgreSQL backup and restore verification cycle.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--backup-dir", default=str(default_backup_dir()))
    parser.add_argument("--upload-dir", default=str(default_upload_dir()))
    parser.add_argument("--restore-upload-root", default=str(BACKEND_DIR / "data" / "restore_test_uploads"))
    parser.add_argument("--admin-password-env", default="E2E_ADMIN_PASSWORD")
    parser.add_argument("--cleanup-restore-db", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    admin_password = os.environ.get(args.admin_password_env)
    if not admin_password:
        raise SystemExit(f"{args.admin_password_env} is required for API login verification")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    data_run_id = f"{run_id}_{uuid.uuid4().hex[:6]}"
    backup_dir = Path(args.backup_dir).resolve()
    upload_dir = Path(args.upload_dir).resolve()
    source_url = to_sync_url(args.database_url)
    require_isolated_test_source(source_url)
    restore_db = f"luxury_shopping_restore_test_{run_id}_{uuid.uuid4().hex[:6]}".lower()
    restore_url = replace_database(source_url, restore_db)
    restore_upload_dir = Path(args.restore_upload_root).resolve() / restore_db
    assert_safe_restore_target(source_url, restore_url)

    cycle_summary: dict[str, Any] = {
        "run_id": data_run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_database": safe_db_info(source_url),
        "restore_database": safe_db_info(restore_url),
        "source_api_port": None,
        "restore_api_port": None,
        "steps": {},
        "ok": False,
    }
    source_process: subprocess.Popen | None = None
    restore_process: subprocess.Popen | None = None
    manifest_path: Path | None = None
    try:
        source_port = int(os.environ.get("SOURCE_TEST_API_PORT") or free_port())
        source_base = f"http://127.0.0.1:{source_port}"
        source_process = start_fastapi(database_url=source_url, upload_dir=upload_dir, port=source_port, log_path=backup_dir / "source_api.log")
        cycle_summary["source_api_port"] = source_port
        seed = create_seed_data(source_base, data_run_id, admin_password)
        seed["_customer_password"] = f"Restore{data_run_id[-6:]}!512"
        seed["_merchant_password"] = f"Merchant{data_run_id[-6:]}!512"
        seed_manifest_path = backup_dir / f"seed_manifest_{data_run_id}.json"
        seed_manifest_path.write_text(json.dumps({key: value for key, value in seed.items() if key != "_customer_password"}, ensure_ascii=False, indent=2), encoding="utf-8")
        cycle_summary["steps"]["seed_data"] = {"ok": True, "manifest": str(seed_manifest_path), "ids": {key: seed[key] for key in seed if key.endswith("_id")}}
        cycle_summary["steps"]["seed_db_check_source"] = verify_seed_ids(source_url, seed)

        backup = create_backup_package(database_url=source_url, backup_dir=backup_dir, upload_dir=upload_dir, created_by="admin-api-cycle", run_id=data_run_id)
        manifest_path = Path(backup["manifest_path"])
        cycle_summary["steps"]["backup"] = {"ok": True, "manifest_path": str(manifest_path), "backup_id": backup["backup_id"]}
        cycle_summary["steps"]["verify_backup"] = verify_backup_manifest(manifest_path)

        restore = restore_backup_package(
            manifest_path=manifest_path,
            source_database_url=source_url,
            restore_database_url=restore_url,
            restore_upload_dir=restore_upload_dir,
        )
        cycle_summary["steps"]["restore"] = restore
        cycle_summary["steps"]["seed_db_check_restore"] = verify_seed_ids(restore_url, seed)
        cycle_summary["steps"]["alembic_restore"] = run_alembic_check(restore_url, manifest_path.parent)

        restore_port = int(os.environ.get("RESTORE_TEST_API_PORT") or free_port())
        restore_base = f"http://127.0.0.1:{restore_port}"
        restore_process = start_fastapi(database_url=restore_url, upload_dir=restore_upload_dir, port=restore_port, log_path=manifest_path.parent / "restore_api.log")
        cycle_summary["restore_api_port"] = restore_port
        cycle_summary["steps"]["restored_application"] = exercise_restored_application(restore_base, restore_url, source_url, seed, admin_password, data_run_id)

        cycle_summary["steps"]["corrupted_backup_detection"] = check_corrupted_backup_detection(manifest_path)
        cycle_summary["steps"]["missing_uploads_detection"] = check_missing_uploads_detection(manifest_path)
        cycle_summary["steps"]["relationships_restore"] = relationship_checks(restore_url)
        cycle_summary["steps"]["uploads_source_after"] = upload_manifest(upload_dir)
        cycle_summary["steps"]["uploads_restore_after"] = upload_manifest(restore_upload_dir)
        cycle_summary["steps"]["uploads_compare_after"] = compare_upload_manifests(
            cycle_summary["steps"]["uploads_source_after"],
            cycle_summary["steps"]["uploads_restore_after"],
        )
        cycle_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        cycle_summary["ok"] = all(
            step.get("ok", False)
            for name, step in cycle_summary["steps"].items()
            if name not in {"uploads_source_after", "uploads_restore_after", "uploads_compare_after"}
        )
        if manifest_path:
            (manifest_path.parent / "full_cycle_result.json").write_text(json.dumps(cycle_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(json.dumps({"ok": cycle_summary["ok"], "cycle_result": str(manifest_path.parent / "full_cycle_result.json"), "manifest_path": str(manifest_path)}, ensure_ascii=False))
        return 0 if cycle_summary["ok"] else 5
    except Exception as error:
        cycle_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        cycle_summary["error"] = str(error)
        output_dir = manifest_path.parent if manifest_path else backup_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full_cycle_result.json").write_text(json.dumps(cycle_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"ok": False, "error": str(error), "cycle_result": str(output_dir / "full_cycle_result.json")}, ensure_ascii=False))
        return 6
    finally:
        stop_process(restore_process)
        stop_process(source_process)
        if args.cleanup_restore_db:
            try:
                drop_restore_database(source_url, restore_url)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
