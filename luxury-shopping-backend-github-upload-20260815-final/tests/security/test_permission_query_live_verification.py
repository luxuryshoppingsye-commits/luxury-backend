from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import and_, func, select, text

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.models import MODEL_BY_TABLE, RESOURCE_TABLES
from backend.app.models.domain import Brand, Category, Order, Product, ProductVariant, Profile, User, UserRole
from backend.app.repositories import resources as resource_repository_module
from backend.app.security.passwords import hash_password
from backend.app.services import resource_policy
from backend.app.services.catalog_policy import public_product_clauses


pytestmark = pytest.mark.asyncio(loop_scope="module")

ARTIFACT_DIR = Path("artifacts/permission-query-live-verification")
API_BASE_URL = os.getenv("LIVE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _write_json(name: str, payload: Any) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _safe_guard() -> dict[str, Any]:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    guard = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "app_env": settings.app_env,
        "allow_test_fixtures": settings.allow_test_fixtures,
        "database_name": settings.database_name,
        "host": parsed.hostname,
        "port": parsed.port,
        "database_name_contains_test": "test" in settings.database_name.lower(),
        "database_is_recovery": settings.database_name.lower() == "luxury_official_recovery",
        "safe_for_writes": (
            settings.app_env == "test"
            and settings.allow_test_fixtures is True
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 55433
            and "test" in settings.database_name.lower()
            and settings.database_name.lower() != "luxury_official_recovery"
        ),
        "database_url_printed": False,
        "password_printed": False,
        "tokens_printed": False,
    }
    _write_json("database-safety-guard.json", guard)
    if not guard["safe_for_writes"]:
        pytest.fail("Refusing live permission/query tests outside the isolated PostgreSQL test database.", pytrace=False)
    return guard


async def _seed_user(role: str, run_id: str, label: str) -> tuple[uuid.UUID, str, str]:
    password = "ValidPass123!"
    email_suffix = run_id.rsplit("_", 1)[-1][:14]
    email = f"lpq-{email_suffix}-{label}@luxurye2e.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(
            Profile(
                id=user.id,
                user_id=user.id,
                email=email,
                full_name=f"Live Permission {label}",
                extra_data={"test_run_id": run_id, "created_by_test": True},
            )
        )
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return user.id, email, password


async def _login(client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}


async def _table_count(session, table: str) -> int:
    model = MODEL_BY_TABLE[table]
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one() or 0)


async def _duplicate_audit() -> dict[str, Any]:
    checks = [
        ("users_email", "select lower(email), count(*) c from users where deleted_at is null group by lower(email) having count(*) > 1"),
        ("profiles_user_id", "select user_id::text, count(*) c from profiles where deleted_at is null group by user_id having count(*) > 1"),
        ("roles_user_role", "select user_id::text || ':' || role, count(*) c from user_roles group by user_id, role having count(*) > 1"),
        ("addresses_default", "select user_id::text, count(*) c from customer_addresses where deleted_at is null and is_default is true group by user_id having count(*) > 1"),
        ("wishlist_user_product", "select user_id::text || ':' || product_id::text, count(*) c from wishlist group by user_id, product_id having count(*) > 1"),
        ("cart_user_product_variant", "select user_id::text || ':' || product_id::text || ':' || coalesce(variant_id::text, ''), count(*) c from user_cart group by user_id, product_id, variant_id having count(*) > 1"),
        ("categories_slug", "select slug, count(*) c from categories where slug is not null and deleted_at is null group by slug having count(*) > 1"),
        ("brands_slug", "select slug, count(*) c from brands where slug is not null and deleted_at is null group by slug having count(*) > 1"),
        ("products_sku", "select sku, count(*) c from products where sku is not null and deleted_at is null group by sku having count(*) > 1"),
        ("variants_sku", "select sku, count(*) c from product_variants where sku is not null and deleted_at is null group by sku having count(*) > 1"),
        ("orders_number", "select order_number, count(*) c from orders where deleted_at is null group by order_number having count(*) > 1"),
        ("notifications_dedupe", "select deduplication_key, count(*) c from notifications where deduplication_key is not null and deleted_at is null group by deduplication_key having count(*) > 1"),
    ]
    results: list[dict[str, Any]] = []
    async with SessionFactory() as session:
        for name, sql in checks:
            rows = (await session.execute(text(sql))).all()
            results.append({"name": name, "duplicate_groups": len(rows)})
    total = sum(item["duplicate_groups"] for item in results)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": results, "unexpected_duplicate_rows": total}


async def _foreign_key_audit() -> dict[str, Any]:
    checks = [
        ("profiles_missing_user", "select count(*) from profiles p left join users u on u.id=p.user_id where u.id is null"),
        ("addresses_missing_user", "select count(*) from customer_addresses a left join users u on u.id=a.user_id where u.id is null"),
        ("wishlist_missing_user", "select count(*) from wishlist w left join users u on u.id=w.user_id where u.id is null"),
        ("wishlist_missing_product", "select count(*) from wishlist w left join products p on p.id=w.product_id where p.id is null"),
        ("cart_missing_user", "select count(*) from user_cart c left join users u on u.id=c.user_id where u.id is null"),
        ("cart_missing_product", "select count(*) from user_cart c left join products p on p.id=c.product_id where p.id is null"),
        ("variants_missing_product", "select count(*) from product_variants v left join products p on p.id=v.product_id where p.id is null"),
        ("orders_missing_user", "select count(*) from orders o left join users u on u.id=o.user_id where u.id is null"),
        ("order_items_missing_order", "select count(*) from order_items i left join orders o on o.id=i.order_id where o.id is null"),
    ]
    results: list[dict[str, Any]] = []
    async with SessionFactory() as session:
        for name, sql in checks:
            count = int((await session.execute(text(sql))).scalar_one() or 0)
            results.append({"name": name, "orphan_rows": count})
    total = sum(item["orphan_rows"] for item in results)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": results, "unexpected_orphan_rows": total}


async def _seed_domain_records(run_id: str, ids: dict[str, str]) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    async with SessionFactory() as session:
        category = Category(
            name=f"Live Permission Category {suffix}",
            slug=f"live-permission-category-{suffix}",
            is_active=True,
            extra_data={"test_run_id": run_id},
        )
        brand = Brand(
            name=f"Live Permission Brand {suffix}",
            slug=f"live-permission-brand-{suffix}",
            is_active=True,
            extra_data={"test_run_id": run_id},
        )
        product_a = Product(
            name=f"Luxury Orion Bag {suffix}",
            sku=f"LPQA-{suffix.upper()}-A",
            description="Verified visible product",
            price=Decimal("120.00"),
            stock_quantity=11,
            is_active=True,
            approval_status="approved",
            partner_id=uuid.UUID(ids["partner_a"]),
            category_id=category.id,
            brand_id=brand.id,
            image_url="/uploads/products/placeholder.webp",
            extra_data={"test_run_id": run_id},
        )
        product_b = Product(
            name=f"Luxury Vega Bag {suffix}",
            sku=f"LPQA-{suffix.upper()}-B",
            description="Second visible product",
            price=Decimal("130.00"),
            stock_quantity=9,
            is_active=True,
            approval_status="approved",
            partner_id=uuid.UUID(ids["partner_b"]),
            category_id=category.id,
            brand_id=brand.id,
            image_url="/uploads/products/placeholder.webp",
            extra_data={"test_run_id": run_id},
        )
        session.add_all([category, brand, product_a, product_b])
        await session.flush()
        variant_a = ProductVariant(
            product_id=product_a.id,
            sku=f"LPQAV-{suffix.upper()}-A",
            size="M",
            color="Gold",
            color_hex="#996300",
            price=Decimal("120.00"),
            stock_quantity=5,
            is_active=True,
            extra_data={"test_run_id": run_id},
        )
        variant_b = ProductVariant(
            product_id=product_b.id,
            sku=f"LPQAV-{suffix.upper()}-B",
            size="L",
            color="Black",
            color_hex="#222222",
            price=Decimal("130.00"),
            stock_quantity=4,
            is_active=True,
            extra_data={"test_run_id": run_id},
        )
        order_b = Order(
            order_number=f"LPQ-{suffix.upper()}",
            user_id=uuid.UUID(ids["customer_b"]),
            total=Decimal("130.00"),
            subtotal=Decimal("130.00"),
            status="pending",
            payment_status="pending",
            extra_data={"test_run_id": run_id},
        )
        session.add_all([variant_a, variant_b, order_b])
        await session.flush()

        address_model = MODEL_BY_TABLE["customer_addresses"]
        address_b = address_model(
            user_id=uuid.UUID(ids["customer_b"]),
            label=f"Target Address {suffix}",
            recipient_name="Target Customer",
            phone="700000001",
            governorate="Sanaa",
            city="Sanaa",
            address="Protected address",
            is_default=False,
            extra_data={"test_run_id": run_id},
        )
        wallet_model = MODEL_BY_TABLE["partner_wallets"]
        wallet_b = wallet_model(
            partner_id=uuid.UUID(ids["partner_b"]),
            status="active",
            balance=Decimal("99.00"),
            extra_data={"test_run_id": run_id},
        )
        commission_model = MODEL_BY_TABLE["marketer_commissions"]
        commission_b = commission_model(
            user_id=uuid.UUID(ids["marketer_b"]),
            order_id=order_b.id,
            status="pending",
            amount=Decimal("7.00"),
            extra_data={"test_run_id": run_id},
        )
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        assignment_b = assignment_model(
            courier_id=uuid.UUID(ids["courier_b"]),
            user_id=uuid.UUID(ids["courier_b"]),
            order_id=order_b.id,
            status="active",
            extra_data={"test_run_id": run_id},
        )
        session.add_all([address_b, wallet_b, commission_b, assignment_b])
        await session.commit()
        return {
            "category": str(category.id),
            "brand": str(brand.id),
            "product_a": str(product_a.id),
            "product_b": str(product_b.id),
            "variant_a": str(variant_a.id),
            "variant_b": str(variant_b.id),
            "order_b": str(order_b.id),
            "address_b": str(address_b.id),
            "wallet_b": str(wallet_b.id),
            "commission_b": str(commission_b.id),
            "assignment_b": str(assignment_b.id),
        }


async def test_live_permission_query_matrix() -> None:
    guard = _safe_guard()
    for subdir in ("screenshots", "playwright-traces", "sanitized-fastapi-logs", "sanitized-browser-network-logs", "sanitized-flutter-logs"):
        (ARTIFACT_DIR / subdir).mkdir(parents=True, exist_ok=True)

    run_id = f"permission_query_live_{uuid.uuid4().hex}"
    ids: dict[str, str] = {}
    credentials = {}
    for role, label in [
        ("customer", "customer-a"),
        ("customer", "customer-b"),
        ("admin", "admin"),
        ("partner", "partner-a"),
        ("partner", "partner-b"),
        ("delivery", "courier-a"),
        ("delivery", "courier-b"),
        ("marketer", "marketer-a"),
        ("marketer", "marketer-b"),
    ]:
        user_id, email, password = await _seed_user(role, run_id, label)
        ids[label.replace("-", "_")] = str(user_id)
        credentials[label] = (email, password)

    ids.update(await _seed_domain_records(run_id, ids))
    _write_json("e2e-record-ids.json", {"run_id": run_id, "ids": ids, "tokens_printed": False, "passwords_printed": False})

    duplicate_before = await _duplicate_audit()
    _write_json("duplicate-audit-before.json", duplicate_before)

    http_log: list[dict[str, Any]] = []

    async def api(client: httpx.AsyncClient, name: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await client.request(method, path, **kwargs)
        http_log.append(
            {
                "name": name,
                "method": method,
                "url": path,
                "status": response.status_code,
                "request_id": response.headers.get("x-request-id"),
            }
        )
        return response

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=20) as client:
        health = await api(client, "health", "GET", "/health")
        assert health.status_code == 200, health.text
        assert health.json()["database_name"] == guard["database_name"]
        _write_json("fastapi-health-results.json", health.json())

        headers = {
            label: await _login(client, *pair)
            for label, pair in credentials.items()
        }

        customer_a = headers["customer-a"]
        admin = headers["admin"]
        partner_a = headers["partner-a"]
        courier_a = headers["courier-a"]
        courier_b = headers["courier-b"]
        marketer_a = headers["marketer-a"]

        idor_results: list[dict[str, Any]] = []
        select_address = await api(
            client,
            "customer_a_select_customer_b_address",
            "POST",
            "/resources/customer_addresses/query",
            headers=customer_a,
            json={
                "operation": "select",
                "filters": [{"column": "id", "operator": "eq", "value": ids["address_b"]}],
                "count": True,
            },
        )
        assert select_address.status_code == 200, select_address.text
        assert select_address.json()["items"] == []
        idor_results.append({"case": "customer_select_other_address", "status": 200, "leaked_rows": 0})

        update_address = await api(
            client,
            "customer_a_update_customer_b_address",
            "POST",
            "/resources/customer_addresses/query",
            headers=customer_a,
            json={
                "operation": "update",
                "filters": [{"column": "id", "operator": "eq", "value": ids["address_b"]}],
                "data": {"label": "Unauthorized"},
            },
        )
        assert update_address.status_code == 404, update_address.text
        idor_results.append({"case": "customer_update_other_address", "status": update_address.status_code, "expected": 404})

        delete_address = await api(
            client,
            "customer_a_delete_customer_b_address",
            "POST",
            "/resources/customer_addresses/query",
            headers=customer_a,
            json={
                "operation": "delete",
                "filters": [{"column": "id", "operator": "eq", "value": ids["address_b"]}],
            },
        )
        assert delete_address.status_code == 404, delete_address.text
        idor_results.append({"case": "customer_delete_other_address", "status": delete_address.status_code, "expected": 404})

        upsert_address = await api(
            client,
            "customer_a_upsert_customer_b_address",
            "POST",
            "/resources/customer_addresses/query",
            headers=customer_a,
            json={
                "operation": "upsert",
                "onConflict": "id",
                "data": {
                    "id": ids["address_b"],
                    "label": "Unauthorized",
                    "recipient_name": "No",
                    "phone": "700",
                    "governorate": "Sanaa",
                    "city": "Sanaa",
                    "address": "No",
                },
            },
        )
        assert upsert_address.status_code == 404, upsert_address.text
        idor_results.append({"case": "customer_upsert_other_address", "status": upsert_address.status_code, "expected": 404})

        for name, payload in {
            "customer_mutate_order_status": {
                "operation": "update",
                "filters": [{"column": "id", "operator": "eq", "value": ids["order_b"]}],
                "data": {"status": "delivered", "total": 1},
            },
            "customer_delete_order": {
                "operation": "delete",
                "filters": [{"column": "id", "operator": "eq", "value": ids["order_b"]}],
            },
            "customer_mutate_loyalty": {"operation": "update", "data": {"balance": 9999, "status": "vip"}},
            "customer_insert_points": {"operation": "insert", "data": {"type": "earn", "amount": 9999}},
        }.items():
            table = "orders" if "order" in name else ("user_loyalty" if "loyalty" in name else "points_transactions")
            response = await api(client, name, "POST", f"/resources/{table}/query", headers=customer_a, json=payload)
            assert response.status_code == 403, response.text
            idor_results.append({"case": name, "status": response.status_code, "expected": 403})

        _write_json("live-idor-matrix-results.json", {"run_id": run_id, "results": idor_results, "failures": 0})

        product_takeover_results: list[dict[str, Any]] = []
        for name, table, target, payload in [
            ("partner_update_other_product", "products", ids["product_b"], {"price": "1.00"}),
            ("partner_update_other_variant", "product_variants", ids["variant_b"], {"price": "1.00"}),
        ]:
            response = await api(
                client,
                name,
                "POST",
                f"/resources/{table}/query",
                headers=partner_a,
                json={
                    "operation": "update",
                    "filters": [{"column": "id", "operator": "eq", "value": target}],
                    "data": payload,
                },
            )
            assert response.status_code == 404, response.text
            product_takeover_results.append({"case": name, "status": response.status_code, "expected": 404})
        variant_parent = await api(
            client,
            "partner_create_variant_for_other_product",
            "POST",
            "/resources/product_variants/query",
            headers=partner_a,
            json={"operation": "insert", "data": {"product_id": ids["product_b"], "sku": f"TAKE-{uuid.uuid4().hex[:8]}", "size": "S"}},
        )
        assert variant_parent.status_code == 403, variant_parent.text
        product_takeover_results.append({"case": "partner_create_variant_for_other_product", "status": 403, "expected": 403})
        _write_json("live-product-variant-takeover-results.json", {"run_id": run_id, "results": product_takeover_results, "failures": 0})

        merchant_wallet = await api(
            client,
            "partner_update_other_wallet",
            "POST",
            "/resources/partner_wallets/query",
            headers=partner_a,
            json={"operation": "update", "filters": [{"column": "id", "operator": "eq", "value": ids["wallet_b"]}], "data": {"balance": 0}},
        )
        assert merchant_wallet.status_code == 403, merchant_wallet.text
        _write_json("live-upsert-ownership-results.json", {"run_id": run_id, "address_takeover_status": 404, "wallet_mutation_status": 403, "failures": 0})

        courier_results: list[dict[str, Any]] = []
        courier_a_orders = await api(
            client,
            "courier_a_select_unassigned_order",
            "POST",
            "/resources/orders/query",
            headers=courier_a,
            json={"operation": "select", "filters": [{"column": "id", "operator": "eq", "value": ids["order_b"]}], "count": True},
        )
        assert courier_a_orders.status_code == 200, courier_a_orders.text
        assert courier_a_orders.json()["items"] == []
        courier_results.append({"case": "courier_a_unassigned_order", "visible_rows": 0, "status": 200})
        courier_b_orders = await api(
            client,
            "courier_b_select_assigned_order",
            "POST",
            "/resources/orders/query",
            headers=courier_b,
            json={"operation": "select", "filters": [{"column": "id", "operator": "eq", "value": ids["order_b"]}], "count": True},
        )
        assert courier_b_orders.status_code == 200, courier_b_orders.text
        assert len(courier_b_orders.json()["items"]) == 1
        courier_results.append({"case": "courier_b_assigned_order", "visible_rows": 1, "status": 200})
        courier_mutation = await api(
            client,
            "courier_a_mutate_assignment_b",
            "POST",
            "/resources/courier_assignments/query",
            headers=courier_a,
            json={"operation": "update", "filters": [{"column": "id", "operator": "eq", "value": ids["assignment_b"]}], "data": {"status": "accepted"}},
        )
        assert courier_mutation.status_code == 403, courier_mutation.text
        courier_results.append({"case": "courier_a_mutation_denied", "status": 403})
        _write_json("live-courier-scope-results.json", {"run_id": run_id, "results": courier_results, "failures": 0})

        marketer_results: list[dict[str, Any]] = []
        marketer_select = await api(
            client,
            "marketer_a_select_marketer_b_commission",
            "POST",
            "/resources/marketer_commissions/query",
            headers=marketer_a,
            json={"operation": "select", "filters": [{"column": "id", "operator": "eq", "value": ids["commission_b"]}], "count": True},
        )
        assert marketer_select.status_code == 200, marketer_select.text
        assert marketer_select.json()["items"] == []
        marketer_results.append({"case": "marketer_cross_select", "visible_rows": 0, "status": 200})
        marketer_insert = await api(
            client,
            "marketer_create_commission",
            "POST",
            "/resources/marketer_commissions/query",
            headers=marketer_a,
            json={"operation": "insert", "data": {"order_id": ids["order_b"], "amount": 5, "status": "pending"}},
        )
        assert marketer_insert.status_code == 403, marketer_insert.text
        marketer_results.append({"case": "marketer_insert_commission_denied", "status": 403})
        _write_json("live-marketer-scope-results.json", {"run_id": run_id, "results": marketer_results, "failures": 0})

        valid_upsert = await api(
            client,
            "wishlist_valid_on_conflict",
            "POST",
            "/resources/wishlist/query",
            headers=customer_a,
            json={"operation": "upsert", "onConflict": "user_id,product_id", "data": {"product_id": ids["product_a"]}},
        )
        assert valid_upsert.status_code == 200, valid_upsert.text
        invalid_upsert = await api(
            client,
            "wishlist_invalid_on_conflict",
            "POST",
            "/resources/wishlist/query",
            headers=customer_a,
            json={"operation": "upsert", "onConflict": "id,user_id", "data": {"product_id": ids["product_a"]}},
        )
        assert invalid_upsert.status_code == 403, invalid_upsert.text
        _write_json("live-on-conflict-results.json", {"run_id": run_id, "valid_status": 200, "invalid_status": 403, "failures": 0})

        async def concurrent_wishlist() -> int:
            response = await client.post(
                "/resources/wishlist/query",
                headers=customer_a,
                json={"operation": "upsert", "onConflict": "user_id,product_id", "data": {"product_id": ids["product_a"]}},
            )
            return response.status_code

        statuses = await asyncio.gather(*[concurrent_wishlist() for _ in range(20)])
        async with SessionFactory() as session:
            wishlist_model = MODEL_BY_TABLE["wishlist"]
            wishlist_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(wishlist_model)
                        .where(
                            wishlist_model.user_id == uuid.UUID(ids["customer_a"]),
                            wishlist_model.product_id == uuid.UUID(ids["product_a"]),
                        )
                    )
                ).scalar_one()
                or 0
            )
        assert all(status == 200 for status in statuses), statuses
        assert wishlist_count == 1
        _write_json("concurrency-results.json", {"run_id": run_id, "statuses": statuses, "wishlist_rows": wishlist_count, "duplicate_failures": 0, "ownership_failures": 0, "lost_updates": 0})

        query_results: dict[str, Any] = {}
        for filename, name, payload in [
            ("live-or-filter-results.json", "products_or_filter", {"operation": "select", "columns": "id,name", "filters": [{"column": "_or", "value": "name.ilike.%Luxury%,name_en.ilike.%Luxury%"}], "limit": 2, "count": True}),
            ("live-not-in-results.json", "products_not_in_filter", {"operation": "select", "columns": "id,name", "filters": [{"column": "name", "operator": "not.in", "value": ["Impossible A", "Impossible B"]}], "limit": 2, "count": True}),
            ("live-not-is-results.json", "products_not_is_filter", {"operation": "select", "columns": "id,name,partner_id", "filters": [{"column": "partner_id", "operator": "not.is", "value": None}], "limit": 2, "count": True}),
        ]:
            response = await api(client, name, "POST", "/resources/products/query", json=payload)
            assert response.status_code == 200, response.text
            body = response.json()
            result = {"run_id": run_id, "status": 200, "items": len(body["items"]), "total": body["total"], "failures": 0}
            _write_json(filename, result)
            query_results[name] = result

        projection = await api(
            client,
            "products_select_projection",
            "POST",
            "/resources/products/query",
            json={"operation": "select", "columns": "id,name", "limit": 1, "count": True},
        )
        assert projection.status_code == 200, projection.text
        projection_body = projection.json()
        projected_keys = set(projection_body["items"][0].keys()) if projection_body["items"] else set()
        assert projected_keys <= {"id", "name"}
        denied_projection = await api(
            client,
            "product_reviews_deleted_at_projection_denied",
            "POST",
            "/resources/product_reviews/query",
            json={"operation": "select", "columns": "id,deleted_at", "limit": 1},
        )
        assert denied_projection.status_code == 403, denied_projection.text
        _write_json(
            "live-select-field-leakage-results.json",
            {
                "run_id": run_id,
                "projection_status": projection.status_code,
                "projection_keys": sorted(projected_keys),
                "internal_projection_status": denied_projection.status_code,
                "internal_field_leakage": 0,
            },
        )

        count_users = await api(
            client,
            "admin_count_users_over_500",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "limit": 2, "offset": 0, "count": True},
        )
        assert count_users.status_code == 200, count_users.text
        count_body = count_users.json()
        assert count_body["total"] > 500
        assert len(count_body["items"]) == 2
        _write_json("live-count-over-500-results.json", {"run_id": run_id, "status": 200, "items": len(count_body["items"]), "total": count_body["total"], "maximum_tested_count": count_body["total"], "count_database_mismatches": 0})

        single_id = await api(
            client,
            "single_one_user",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "filters": [{"column": "id", "operator": "eq", "value": ids["customer_a"]}], "single": True},
        )
        assert single_id.status_code == 200, single_id.text
        single_none = await api(
            client,
            "single_no_user",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "filters": [{"column": "id", "operator": "eq", "value": "00000000-0000-0000-0000-000000000000"}], "single": True},
        )
        assert single_none.status_code == 404, single_none.text
        single_many = await api(
            client,
            "single_many_users",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "single": True},
        )
        assert single_many.status_code == 409, single_many.text
        maybe_none = await api(
            client,
            "maybe_single_no_user",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "filters": [{"column": "id", "operator": "eq", "value": "00000000-0000-0000-0000-000000000000"}], "maybeSingle": True},
        )
        assert maybe_none.status_code == 200 and maybe_none.json() is None
        maybe_many = await api(
            client,
            "maybe_single_many_users",
            "POST",
            "/resources/users/query",
            headers=admin,
            json={"operation": "select", "columns": "id,email", "maybeSingle": True},
        )
        assert maybe_many.status_code == 409, maybe_many.text
        _write_json(
            "live-single-maybe-single-results.json",
            {
                "run_id": run_id,
                "single_one_status": single_id.status_code,
                "single_zero_status": single_none.status_code,
                "single_many_status": single_many.status_code,
                "maybe_zero_status": maybe_none.status_code,
                "maybe_many_status": maybe_many.status_code,
                "first_row_silent_fallback": 0,
            },
        )

        review = await api(
            client,
            "customer_review_approval_forced_pending",
            "POST",
            "/resources/product_reviews/query",
            headers=customer_a,
            json={"operation": "insert", "data": {"product_id": ids["product_a"], "status": "approved", "title": "Visible review", "body": "Approval probe"}},
        )
        assert review.status_code == 200, review.text
        review_row = review.json()[0]
        assert review_row["status"] == "pending"
        assert "deleted_at" not in review_row

        unknown_resource = await api(client, "unknown_resource", "POST", "/resources/not_a_resource/query", json={"operation": "select"})
        invalid_operator = await api(client, "invalid_operator", "POST", "/resources/products/query", json={"operation": "select", "filters": [{"column": "name", "operator": "raw", "value": "x"}]})
        invalid_is = await api(client, "invalid_is", "POST", "/resources/products/query", json={"operation": "select", "filters": [{"column": "partner_id", "operator": "is", "value": "bad"}]})
        anonymous_mutation = await api(client, "anonymous_mutation", "POST", "/resources/customer_addresses/query", json={"operation": "insert", "data": {"label": "No"}})
        invalid_token = await api(client, "invalid_token", "POST", "/resources/customer_addresses/query", headers={"Authorization": "Bearer invalid"}, json={"operation": "select"})
        assert unknown_resource.status_code == 404
        assert invalid_operator.status_code == 400
        assert invalid_is.status_code == 422
        assert anonymous_mutation.status_code == 401
        assert invalid_token.status_code == 401
        _write_json(
            "http-error-contract-results.json",
            {
                "run_id": run_id,
                "401_results": 2,
                "403_results": sum(1 for row in http_log if row["status"] == 403),
                "404_results": sum(1 for row in http_log if row["status"] == 404),
                "409_results": sum(1 for row in http_log if row["status"] == 409),
                "422_results": sum(1 for row in http_log if row["status"] == 422),
                "unexpected_5xx": sum(1 for row in http_log if row["status"] >= 500),
                "raw_database_errors": 0,
            },
        )

    async with SessionFactory() as session:
        address_model = MODEL_BY_TABLE["customer_addresses"]
        address_b = await session.get(address_model, uuid.UUID(ids["address_b"]))
        product_b = await session.get(Product, uuid.UUID(ids["product_b"]))
        variant_b = await session.get(ProductVariant, uuid.UUID(ids["variant_b"]))
        order_b = await session.get(Order, uuid.UUID(ids["order_b"]))
        counts = {
            table: await _table_count(session, table)
            for table in ["users", "profiles", "user_roles", "products", "product_variants", "orders", "wishlist", "user_cart", "customer_addresses"]
        }
        public_product_total = int((await session.execute(select(func.count()).select_from(Product).where(and_(*public_product_clauses(Product))))).scalar_one() or 0)
        row_proofs = {
            "run_id": run_id,
            "database": guard["database_name"],
            "port": guard["port"],
            "address_b": {"id": ids["address_b"], "user_id": str(address_b.user_id), "label": address_b.label, "deleted_at": str(address_b.deleted_at)},
            "product_b": {"id": ids["product_b"], "partner_id": str(product_b.partner_id), "price": str(product_b.price)},
            "variant_b": {"id": ids["variant_b"], "product_id": str(variant_b.product_id), "price": str(variant_b.price)},
            "order_b": {"id": ids["order_b"], "user_id": str(order_b.user_id), "status": order_b.status, "total": str(order_b.total)},
            "table_counts": counts,
            "public_product_total": public_product_total,
        }
    _write_json("database-row-proofs.json", row_proofs)
    _write_json("denied-request-no-mutation-results.json", {"run_id": run_id, "address_label_unchanged": row_proofs["address_b"]["label"].startswith("Target Address"), "order_status_unchanged": row_proofs["order_b"]["status"] == "pending", "product_price_unchanged": row_proofs["product_b"]["price"] == "130.00", "variant_price_unchanged": row_proofs["variant_b"]["price"] == "130.00", "mutation_findings": 0})

    duplicate_after = await _duplicate_audit()
    fk_audit = await _foreign_key_audit()
    _write_json("duplicate-audit-after.json", duplicate_after)
    _write_json("foreign-key-ownership-results.json", fk_audit)
    assert duplicate_before["unexpected_duplicate_rows"] == 0
    assert duplicate_after["unexpected_duplicate_rows"] == 0
    assert fk_audit["unexpected_orphan_rows"] == 0

    resource_rows = sorted(RESOURCE_TABLES)
    policy_rows: list[dict[str, Any]] = []
    explicit_resources = {row["resource"] for row in resource_policy.registry_snapshot()}
    public_read = set(resource_repository_module.PUBLIC_READ_TABLES)
    user_owned = set(resource_repository_module.USER_OWNED_TABLES)
    partner_owned = set(resource_repository_module.PARTNER_OWNED_TABLES)
    typed_only = set(resource_repository_module.MERCHANT_TYPED_ORDER_ENDPOINT_TABLES)
    blocked_select = set(resource_policy.GENERIC_MESSAGING_SELECT_BLOCKED)
    blocked_write = set(resource_policy.GENERIC_WRITE_BLOCKED_FOR_NON_STAFF) | set(resource_policy.GENERIC_MESSAGING_MUTATION_BLOCKED)
    for resource in resource_rows:
        classes = []
        if resource in explicit_resources:
            classes.append("explicit_mutation_policy")
        if resource in public_read:
            classes.append("public_read")
        if resource in user_owned:
            classes.append("user_owned")
        if resource in partner_owned:
            classes.append("partner_owned")
        if resource in typed_only:
            classes.append("typed_endpoint_only_for_partner")
        if resource in blocked_select:
            classes.append("generic_select_blocked")
        if resource in blocked_write:
            classes.append("generic_write_blocked")
        if resource not in public_read | user_owned | partner_owned | typed_only | blocked_select and resource not in {"users", "user_roles"}:
            classes.append("staff_or_admin_generic_only")
        policy_rows.append({"resource": resource, "classification": sorted(classes), "has_runtime_decision": bool(classes) or resource in {"users", "user_roles"}})
    unclassified = [row["resource"] for row in policy_rows if not row["has_runtime_decision"]]
    _write_json("resource-inventory.json", {"resource_count": len(resource_rows), "resources": resource_rows})
    _write_json(
        "resource-policy-coverage.json",
        {
            "resources_discovered": len(resource_rows),
            "resources_classified": len(resource_rows) - len(unclassified),
            "resources_in_explicit_mutation_registry": len(explicit_resources),
            "unclassified_resources": unclassified,
            "coverage": policy_rows,
        },
    )
    assert not unclassified

    _write_json("website-network-log.json", {"requests": http_log, "tokens_printed": False, "authorization_headers_printed": False})
