from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Brand, Category, Order, OrderItem, Product, ProductVariant, Profile, User, UserRole
from backend.app.security.passwords import hash_password


INTERNAL_PREFIX = "LSH_PERFORMANCE"

CATEGORY_NAMES = [
    ("إلكترونيات", "Electronics", "electronics"),
    ("أزياء", "Fashion", "fashion"),
    ("المنزل والمطبخ", "Home and Kitchen", "home-kitchen"),
    ("العناية والجمال", "Beauty and Care", "beauty-care"),
    ("إكسسوارات فاخرة", "Luxury Accessories", "luxury-accessories"),
]

PRODUCT_NAMES = [
    ("سماعة لاسلكية احترافية", "Professional Wireless Headphones"),
    ("ساعة ذكية رياضية", "Sport Smart Watch"),
    ("حقيبة جلدية أنيقة", "Elegant Leather Bag"),
    ("مصباح مكتبي فاخر", "Premium Desk Lamp"),
    ("عطر شرقي فاخر", "Luxury Oriental Perfume"),
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    return max(int(raw), 0)


def _resource(table: str, **values: Any) -> Any:
    model = MODEL_BY_TABLE[table]
    columns = model.__table__.c
    direct = {key: value for key, value in values.items() if key in columns and value is not None}
    extra = {key: value for key, value in values.items() if key not in columns and value is not None}
    if "extra_data" in columns and extra:
        direct["extra_data"] = extra
    return model(**direct)


async def _ensure_user(session, email: str, role: str, full_name: str, phone: str, password_hash: str) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email, password_hash=password_hash, is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, full_name=full_name, email=email, phone=phone, city="Sanaa"))
    role_row = await session.get(UserRole, {"user_id": user.id, "role": role})
    if role_row is None:
        session.add(UserRole(user_id=user.id, role=role))
    return user


def _write_test_files(run_id: str, count: int) -> list[str]:
    settings = get_settings()
    folder = settings.resolved_upload_dir / "catalog-media" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
    )
    paths = []
    for index in range(max(count, 1)):
        path = folder / f"image-{index:04d}.png"
        if not path.exists():
            path.write_bytes(png)
        paths.append(f"/uploads/catalog-media/{run_id}/{path.name}")
    return paths


async def seed(run_id: str, output: Path) -> dict[str, Any]:
    customer_count = _env_int("PERF_SEED_CUSTOMERS", 50)
    partner_count = _env_int("PERF_SEED_PARTNERS", 5)
    admin_count = _env_int("PERF_SEED_ADMINS", 2)
    category_count = _env_int("PERF_SEED_CATEGORIES", 25)
    brand_count = _env_int("PERF_SEED_BRANDS", 25)
    product_count = _env_int("PERF_SEED_PRODUCTS", 500)
    variant_count = _env_int("PERF_SEED_VARIANTS", product_count * 2)
    historical_orders = _env_int("PERF_SEED_ORDERS", 200)
    notification_count = _env_int("PERF_SEED_NOTIFICATIONS", 500)
    ticket_count = _env_int("PERF_SEED_TICKETS", 100)

    customer_password = os.environ.get("K6_CUSTOMER_PASSWORD", "Performance123")
    staff_password = os.environ.get("K6_STAFF_PASSWORD", customer_password)
    customer_hash = hash_password(customer_password)
    staff_hash = hash_password(staff_password)
    image_paths = _write_test_files(run_id, min(product_count, 200))

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "customers": [],
        "admins": [],
        "partners": [],
        "categories": [],
        "brands": [],
        "products": [],
        "variants": [],
        "counts": {},
    }

    async with SessionFactory() as session:
        customers: list[User] = []
        for index in range(customer_count):
            email = f"lsh.customer.{run_id}.{index:04d}@example.com"
            user = await _ensure_user(session, email, "customer", f"عميل رفاهية {index + 1}", f"71{index:07d}"[-9:], customer_hash)
            customers.append(user)
            manifest["customers"].append({"id": str(user.id), "email": email})

        admins: list[User] = []
        for index in range(admin_count):
            email = f"lsh.admin.{run_id}.{index:04d}@example.com"
            user = await _ensure_user(session, email, "admin", f"مشرف رفاهية {index + 1}", f"77{index:07d}"[-9:], staff_hash)
            admins.append(user)
            manifest["admins"].append({"id": str(user.id), "email": email})

        partners: list[User] = []
        for index in range(partner_count):
            email = f"lsh.partner.{run_id}.{index:04d}@example.com"
            user = await _ensure_user(session, email, "partner", f"شريك رفاهية {index + 1}", f"73{index:07d}"[-9:], staff_hash)
            partners.append(user)
            manifest["partners"].append({"id": str(user.id), "email": email})

        categories: list[Category] = []
        for index in range(category_count):
            category_name_ar, category_name_en, category_slug = CATEGORY_NAMES[index % len(CATEGORY_NAMES)]
            category = Category(
                name=f"{category_name_ar} {index + 1}",
                name_en=f"{category_name_en} {index + 1}",
                slug=f"{category_slug}-{run_id}-{index}",
                is_active=True,
                is_featured=index < 10,
                sort_order=index,
            )
            session.add(category)
            categories.append(category)

        brands: list[Brand] = []
        for index in range(brand_count):
            brand = Brand(
                name=f"علامة فاخرة {index + 1}",
                name_en=f"Luxury Brand {index + 1}",
                slug=f"luxury-brand-{run_id}-{index}",
                is_active=True,
                logo_url=image_paths[index % len(image_paths)],
            )
            session.add(brand)
            brands.append(brand)

        await session.flush()
        manifest["categories"] = [{"id": str(row.id), "slug": row.slug} for row in categories]
        manifest["brands"] = [{"id": str(row.id), "slug": row.slug} for row in brands]

        for partner in partners:
            session.add(_resource(
                "partner_storefronts",
                user_id=partner.id,
                partner_id=partner.id,
                name=f"متجر التقنية الحديثة {partners.index(partner) + 1}",
                email=partner.email,
                phone="777000000",
                status="active",
                is_active=True,
                logo_url=image_paths[0],
            ))
            session.add(_resource("partner_wallets", partner_id=partner.id, status="active", balance=Decimal("0")))

        products: list[Product] = []
        for index in range(product_count):
            partner = partners[index % len(partners)] if partners else None
            product_name_ar, product_name_en = PRODUCT_NAMES[index % len(PRODUCT_NAMES)]
            product = Product(
                name=f"{product_name_ar} {index + 1}",
                name_en=f"{product_name_en} {index + 1}",
                sku=f"{INTERNAL_PREFIX}-SKU-{run_id}-{index:05d}",
                description="منتج مختار بعناية ضمن كتالوج رفاهية التسوق.",
                price=Decimal(str(1000 + (index % 200) * 25)),
                original_price=Decimal(str(1500 + (index % 200) * 25)),
                stock_quantity=1000,
                track_inventory=True,
                is_active=True,
                is_featured=index % 5 == 0,
                approval_status="approved",
                category_id=categories[index % len(categories)].id if categories else None,
                brand_id=brands[index % len(brands)].id if brands else None,
                partner_id=partner.id if partner else None,
                image_url=image_paths[index % len(image_paths)],
                images=[image_paths[index % len(image_paths)]],
                tags=["رفاهية", "منتج مختار"],
            )
            session.add(product)
            products.append(product)

        await session.flush()
        manifest["products"] = [{"id": str(row.id), "sku": row.sku} for row in products]

        variants: list[ProductVariant] = []
        for index in range(variant_count):
            product = products[index % len(products)]
            variant = ProductVariant(
                product_id=product.id,
                sku=f"{INTERNAL_PREFIX}-VAR-{run_id}-{index:05d}",
                size=["S", "M", "L", "XL"][index % 4],
                color=["Black", "Gold", "White", "Navy"][index % 4],
                color_hex=["#111111", "#976817", "#ffffff", "#182238"][index % 4],
                price=product.price + Decimal(index % 4) * Decimal("50"),
                stock_quantity=500,
                image_url=product.image_url,
                is_active=True,
                sort_order=index % 4,
            )
            session.add(variant)
            variants.append(variant)

        await session.flush()
        manifest["variants"] = [{"id": str(row.id), "product_id": str(row.product_id), "sku": row.sku} for row in variants]

        for index in range(5):
            session.add(_resource("warehouses", name=f"مستودع رفاهية {index + 1}", status="active", description="مستودع مخصص للتشغيل التجاري", is_active=True))
        for product in products[: min(1000, len(products))]:
            session.add(_resource("inventory", product_id=product.id, quantity=product.stock_quantity, status="active"))

        coupon = _resource(
            "coupons",
            code=f"LUXURY{run_id}".upper()[:120],
            title="خصم خاص",
            status="active",
            amount=Decimal("100"),
            is_active=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        session.add(coupon)

        order_payments = MODEL_BY_TABLE["order_payments"]
        for index in range(historical_orders):
            customer = customers[index % len(customers)]
            product = products[index % len(products)]
            quantity = 1 + (index % 3)
            subtotal = product.price * quantity
            order = Order(
                order_number=f"PERF-{run_id}-{index:06d}",
                user_id=customer.id,
                status="delivered" if index % 4 == 0 else "pending",
                subtotal=subtotal,
                shipping_total=Decimal("500"),
                discount_total=Decimal("0"),
                total=subtotal + Decimal("500"),
                currency_code="YER",
                payment_method="cash",
                payment_status="paid" if index % 4 == 0 else "pending",
                shipping_address={"city": "Sanaa", "street": "شارع الزبيري"},
                idempotency_key=f"{INTERNAL_PREFIX}-ORDER-{run_id}-{index:06d}",
            )
            session.add(order)
            await session.flush()
            session.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                product_name=product.name,
                product_image=product.image_url,
                quantity=quantity,
                unit_price=product.price,
                total_price=subtotal,
                partner_id=product.partner_id,
            ))
            session.add(order_payments(order_id=order.id, status=order.payment_status, type="cash", amount=order.total))
            if index < len(partners):
                session.add(_resource("marketer_commissions", user_id=customers[index % len(customers)].id, order_id=order.id, status="pending", amount=Decimal("20")))

        notification_model = MODEL_BY_TABLE["notifications"]
        for index in range(notification_count):
            customer = customers[index % len(customers)]
            session.add(notification_model(
                user_id=customer.id,
                recipient_id=customer.id,
                title=f"تنبيه حول طلبك {index + 1}",
                body="تم تحديث حالة الطلب بنجاح.",
                message="تم تحديث حالة الطلب بنجاح.",
                type="order_update",
                status="new",
                is_read=False,
            ))

        for index in range(ticket_count):
            customer = customers[index % len(customers)]
            ticket = _resource(
                "support_tickets",
                user_id=customer.id,
                subject=f"استفسار عن الطلب {index + 1}",
                status="open",
                description="يرجى متابعة حالة الطلب وتزويدي بالمستجدات.",
            )
            session.add(ticket)
            await session.flush()
            session.add(_resource("ticket_messages", ticket_id=ticket.id, sender_id=customer.id, message="أحتاج إلى مساعدة بخصوص طلبي.", is_staff=False))

        for customer in customers[: min(50, len(customers))]:
            session.add(_resource("user_loyalty", user_id=customer.id, status="active", balance=Decimal("100")))
            session.add(_resource("points_transactions", user_id=customer.id, type="opening_balance", amount=Decimal("100"), description="نقاط ولاء افتتاحية"))

        await session.commit()

    manifest["counts"] = {
        "customers": customer_count,
        "partners": partner_count,
        "admins": admin_count,
        "categories": category_count,
        "brands": brand_count,
        "products": product_count,
        "variants": variant_count,
        "historical_orders": historical_orders,
        "notifications": notification_count,
        "tickets": ticket_count,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed performance test data into the configured PostgreSQL database.")
    parser.add_argument("--run-id", default=os.environ.get("K6_RUN_ID") or f"perf-{datetime.now():%Y%m%d-%H%M%S}")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "performance" / "data" / "performance-test-data.json"))
    return parser.parse_args()


async def main() -> None:
    try:
        get_settings().require_test_fixtures_enabled("performance seed data")
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    args = parse_args()
    manifest = await seed(args.run_id, Path(args.output))
    print(json.dumps({"run_id": manifest["run_id"], "counts": manifest["counts"], "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
