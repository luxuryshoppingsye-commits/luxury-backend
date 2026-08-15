from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Order, OrderItem, Product, Profile, User, UserCart, UserRole
from backend.app.security.passwords import hash_password


PREFIX = "LSH_CONCURRENCY"


@dataclass
class SeededUser:
    email: str
    password: str
    user_id: uuid.UUID


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _checkout_body(note: str) -> dict[str, Any]:
    return {
        "paymentMethod": "cash",
        "shippingCost": 0,
        "shippingAddress": {"city": "Sanaa", "street": note},
    }


def _safe_database_info() -> dict[str, str | int | None]:
    parsed = urlparse(get_settings().database_url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": parsed.path.lstrip("/"),
    }


async def _seed_user(label: str, role: str = "customer") -> SeededUser:
    password = "ValidPass123"
    safe_label = "".join(ch for ch in label.lower() if ch.isalnum())[:18] or "user"
    email = f"cct-{safe_label}-{uuid.uuid4().hex[:10]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        display_name = "عميل رفاهية" if role == "customer" else "موظف رفاهية"
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=display_name))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return SeededUser(email=email, password=password, user_id=user.id)


async def _seed_product(label: str, stock: int) -> uuid.UUID:
    async with SessionFactory() as session:
        product = Product(
            name="سماعة لاسلكية احترافية",
            sku=f"LSH-CONC-{label}-{uuid.uuid4().hex[:8]}",
            price=100,
            stock_quantity=stock,
            track_inventory=True,
            is_active=True,
            approval_status="approved",
        )
        session.add(product)
        await session.commit()
        return product.id


async def _login(client: httpx.AsyncClient, user: SeededUser) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": user.email, "password": user.password})
    response.raise_for_status()
    return _headers(response.json()["access_token"])


async def _add_cart(client: httpx.AsyncClient, headers: dict[str, str], product_id: uuid.UUID) -> None:
    response = await client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1})
    if response.status_code not in {200, 201}:
        raise AssertionError(f"cart add failed: {response.status_code} {response.text}")


async def _count_orders_by_key(key: str) -> int:
    async with SessionFactory() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.idempotency_key == key)
                )
            ).scalar_one()
        )


async def scenario_double_checkout_same_key(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    user = await _seed_user(f"live_double_{run_id}")
    product_id = await _seed_product(f"live_double_{run_id}", stock=5)
    key = f"{PREFIX}_live_double_{run_id}"
    headers = await _login(client, user)
    await _add_cart(client, headers, product_id)
    request_headers = {**headers, "Idempotency-Key": key}
    first, second = await asyncio.gather(
        client.post("/orders/checkout", headers=request_headers, json=_checkout_body("live-double")),
        client.post("/orders/checkout", headers=request_headers, json=_checkout_body("live-double")),
    )
    order_count = await _count_orders_by_key(key)
    async with SessionFactory() as session:
        product = await session.get(Product, product_id)
    passed = (
        sorted([first.status_code, second.status_code]) == [200, 201]
        and first.json().get("id") == second.json().get("id")
        and order_count == 1
        and product is not None
        and product.stock_quantity == 4
    )
    return {
        "scenario": "double_checkout_same_key",
        "statuses": [first.status_code, second.status_code],
        "order_count": order_count,
        "remaining_stock": product.stock_quantity if product else None,
        "passed": passed,
    }


async def scenario_last_item(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    first_user = await _seed_user(f"live_last_a_{run_id}")
    second_user = await _seed_user(f"live_last_b_{run_id}")
    product_id = await _seed_product(f"live_last_{run_id}", stock=1)
    first_headers = await _login(client, first_user)
    second_headers = await _login(client, second_user)
    await _add_cart(client, first_headers, product_id)
    await _add_cart(client, second_headers, product_id)
    first, second = await asyncio.gather(
        client.post(
            "/orders/checkout",
            headers={**first_headers, "Idempotency-Key": f"{PREFIX}_live_last_a_{run_id}"},
            json=_checkout_body("live-last-a"),
        ),
        client.post(
            "/orders/checkout",
            headers={**second_headers, "Idempotency-Key": f"{PREFIX}_live_last_b_{run_id}"},
            json=_checkout_body("live-last-b"),
        ),
    )
    async with SessionFactory() as session:
        product = await session.get(Product, product_id)
        order_item_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id)
                )
            ).scalar_one()
        )
    passed = (
        sorted([first.status_code, second.status_code]) == [201, 409]
        and product is not None
        and product.stock_quantity == 0
        and order_item_count == 1
    )
    return {
        "scenario": "last_item_purchase",
        "statuses": [first.status_code, second.status_code],
        "remaining_stock": product.stock_quantity if product else None,
        "order_item_count": order_item_count,
        "passed": passed,
    }


async def scenario_same_key_conflicts(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    user = await _seed_user(f"live_conflict_{run_id}")
    product_id = await _seed_product(f"live_conflict_{run_id}", stock=2)
    key = f"{PREFIX}_live_conflict_{run_id}"
    headers = await _login(client, user)
    await _add_cart(client, headers, product_id)
    first = await client.post(
        "/orders/checkout",
        headers={**headers, "Idempotency-Key": key},
        json=_checkout_body("live-conflict-a"),
    )
    second = await client.post(
        "/orders/checkout",
        headers={**headers, "Idempotency-Key": key},
        json=_checkout_body("live-conflict-b"),
    )
    passed = first.status_code == 201 and second.status_code == 409
    return {
        "scenario": "same_key_different_payload",
        "statuses": [first.status_code, second.status_code],
        "second_detail": second.json().get("detail") if second.headers.get("content-type", "").startswith("application/json") else None,
        "passed": passed,
    }


async def scenario_cart_double_add(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    user = await _seed_user(f"live_cart_{run_id}")
    product_id = await _seed_product(f"live_cart_{run_id}", stock=20)
    headers = await _login(client, user)
    first, second = await asyncio.gather(
        client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1}),
        client.post("/cart", headers=headers, json={"productId": str(product_id), "quantity": 1}),
    )
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(UserCart).where(UserCart.user_id == user.user_id, UserCart.product_id == product_id)
            )
        ).scalars().all()
    passed = first.status_code in {200, 201} and second.status_code in {200, 201} and len(rows) == 1 and rows[0].quantity == 2
    return {
        "scenario": "cart_double_add_no_variant",
        "statuses": [first.status_code, second.status_code],
        "cart_lines": len(rows),
        "quantity": rows[0].quantity if rows else None,
        "passed": passed,
    }


async def _postgres_safety_snapshot() -> dict[str, int]:
    async with SessionFactory() as session:
        idle_transactions = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND state = 'idle in transaction'
                        """
                    )
                )
            ).scalar_one()
        )
        waiting_locks = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_locks
                        WHERE NOT granted
                        """
                    )
                )
            ).scalar_one()
        )
        negative_stock = int(
            (
                await session.execute(
                    select(func.count()).select_from(Product).where(Product.stock_quantity < 0)
                )
            ).scalar_one()
        )
    return {
        "idle_transactions": idle_transactions,
        "waiting_locks": waiting_locks,
        "negative_stock_products": negative_stock,
    }


async def main() -> int:
    try:
        get_settings().require_test_fixtures_enabled("concurrency verification data")
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    base_url = os.environ.get("CONCURRENCY_BASE_URL", "http://127.0.0.1:8810").rstrip("/")
    run_id = os.environ.get("CONCURRENCY_RUN_ID", uuid.uuid4().hex[:10])
    started = time.perf_counter()
    results_dir = Path("backend/data/concurrency")
    results_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        health = await client.get("/health")
        scenarios = [
            await scenario_double_checkout_same_key(client, run_id),
            await scenario_last_item(client, run_id),
            await scenario_same_key_conflicts(client, run_id),
            await scenario_cart_double_add(client, run_id),
        ]
    safety = await _postgres_safety_snapshot()
    report = {
        "run_id": run_id,
        "base_url": base_url,
        "database": _safe_database_info(),
        "health_status": health.status_code,
        "health_body": health.json() if health.headers.get("content-type", "").startswith("application/json") else health.text,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "scenarios": scenarios,
        "postgres_safety": safety,
        "passed": health.status_code == 200 and all(item["passed"] for item in scenarios) and safety["negative_stock_products"] == 0,
    }
    output = results_dir / f"concurrency_verification_{run_id}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": report["passed"], "scenarios": scenarios, "postgres_safety": safety}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
