from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Brand, Category, Product, ProductVariant, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(email: str, role: str) -> tuple[uuid.UUID, str]:
    password = "ValidPass123"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=email.split("@")[0]))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return user.id, password


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    return _headers(body["access_token"])


async def _seed_catalog_rows(suffix: str) -> dict[str, uuid.UUID]:
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        category = Category(name=f"Luxury Category {suffix}", slug=f"luxury-category-{suffix}", is_active=True)
        brand = Brand(name=f"Luxury Brand {suffix}", slug=f"luxury-brand-{suffix}", is_active=True)
        session.add_all([category, brand])
        await session.flush()
        rows = {
            "new": Product(
                name=f"Luxury Perfume New {suffix}",
                sku=f"LUXNEW{suffix}".upper(),
                price=Decimal("100.00"),
                stock_quantity=5,
                is_active=True,
                approval_status="approved",
                approved_at=now,
                category_id=category.id,
                brand_id=brand.id,
            ),
            "old": Product(
                name=f"Luxury Perfume Old {suffix}",
                sku=f"LUXOLD{suffix}".upper(),
                price=Decimal("110.00"),
                stock_quantity=4,
                is_active=True,
                approval_status="approved",
                approved_at=now - timedelta(days=120),
                created_at=now - timedelta(days=120),
                category_id=category.id,
                brand_id=brand.id,
            ),
            "imported": Product(
                name=f"Imported product {suffix}",
                sku=f"LUXIMP{suffix}".upper(),
                price=Decimal("115.00"),
                stock_quantity=6,
                is_active=True,
                approval_status="approved",
                approved_at=now - timedelta(days=30),
                created_at=now - timedelta(days=30),
                category_id=category.id,
                brand_id=brand.id,
            ),
            "pending": Product(
                name=f"Luxury Perfume Pending {suffix}",
                sku=f"LUXPEN{suffix}".upper(),
                price=Decimal("120.00"),
                stock_quantity=3,
                is_active=True,
                approval_status="pending",
                category_id=category.id,
                brand_id=brand.id,
            ),
            "inactive": Product(
                name=f"Luxury Perfume Inactive {suffix}",
                sku=f"LUXINA{suffix}".upper(),
                price=Decimal("130.00"),
                stock_quantity=2,
                is_active=False,
                approval_status="approved",
                category_id=category.id,
                brand_id=brand.id,
            ),
            "rejected": Product(
                name=f"Luxury Perfume Rejected {suffix}",
                sku=f"LUXREJ{suffix}".upper(),
                price=Decimal("140.00"),
                stock_quantity=1,
                is_active=True,
                approval_status="rejected",
                category_id=category.id,
                brand_id=brand.id,
            ),
        }
        session.add_all(rows.values())
        await session.commit()
        return {
            "category_id": category.id,
            "brand_id": brand.id,
            **{key: row.id for key, row in rows.items()},
        }


async def test_public_catalog_filters_visibility_pagination_count_and_dto() -> None:
    get_settings().require_test_fixtures_enabled("product merchant remediation tests")
    suffix = uuid.uuid4().hex[:8]
    ids = await _seed_catalog_rows(suffix)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page_one = await client.get(
            "/api/catalog/products",
            params={"page": 1, "page_size": 1, "categoryId": str(ids["category_id"])},
        )
        assert page_one.status_code == 200, page_one.text
        body = page_one.json()
        assert body["total"] == 3
        assert len(body["items"]) == 1
        assert body["total_pages"] == 3
        assert body["has_next"] is True
        forbidden = {
            "approval_notes",
            "approved_by",
            "approved_at",
            "deleted_at",
            "extra_data",
            "min_stock_quantity",
            "partner_id",
            "created_at",
            "updated_at",
        }
        assert forbidden.isdisjoint(body["items"][0])

        page_two = await client.get(
            "/api/catalog/products",
            params={"page": 2, "page_size": 1, "categoryId": str(ids["category_id"])},
        )
        assert page_two.status_code == 200, page_two.text
        assert page_two.json()["has_previous"] is True
        assert len(page_two.json()["items"]) == 1

        all_public = await client.get(
            "/api/catalog/products",
            params={"categoryId": str(ids["category_id"])},
        )
        assert all_public.status_code == 200, all_public.text
        assert all_public.json()["total"] == 3
        assert len(all_public.json()["items"]) == 3
        assert all_public.json()["total_pages"] == 1
        assert all_public.json()["has_next"] is False
        assert {row["id"] for row in all_public.json()["items"]} == {
            str(ids["new"]),
            str(ids["old"]),
            str(ids["imported"]),
        }

        legacy_public = await client.get(
            "/products",
            params={"limit": 1000, "categorySlug": f"luxury-category-{suffix}"},
        )
        assert legacy_public.status_code == 200, legacy_public.text
        assert {row["id"] for row in legacy_public.json()} == {
            str(ids["new"]),
            str(ids["old"]),
            str(ids["imported"]),
        }

        new_only = await client.get(
            "/api/catalog/products",
            params={"new_only": "true", "categoryId": str(ids["category_id"]), "page_size": 20},
        )
        assert new_only.status_code == 200, new_only.text
        assert new_only.json()["total"] == 1
        assert new_only.json()["items"][0]["id"] == str(ids["new"])

        categories = await client.get("/api/catalog/categories")
        assert categories.status_code == 200
        category_row = next(row for row in categories.json()["data"] if row["id"] == str(ids["category_id"]))
        assert category_row["product_count"] == 3


async def test_public_product_details_return_safe_404_for_private_states() -> None:
    get_settings().require_test_fixtures_enabled("product merchant remediation tests")
    suffix = uuid.uuid4().hex[:8]
    ids = await _seed_catalog_rows(suffix)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        visible = await client.get(f"/api/catalog/products/{ids['new']}")
        assert visible.status_code == 200
        assert visible.json()["data"]["id"] == str(ids["new"])

        for key in ("pending", "inactive", "rejected"):
            catalog_detail = await client.get(f"/api/catalog/products/{ids[key]}")
            legacy_detail = await client.get(f"/products/{ids[key]}")
            assert catalog_detail.status_code == 404
            assert legacy_detail.status_code == 404


async def test_merchant_product_create_update_is_pending_and_cannot_feature_or_activate() -> None:
    get_settings().require_test_fixtures_enabled("product merchant remediation tests")
    suffix = uuid.uuid4().hex[:8]
    merchant_email = f"merchant-remediation-{suffix}@example.com"
    merchant_id, merchant_password = await _seed_user(merchant_email, "partner")
    async with SessionFactory() as session:
        storefront_model = MODEL_BY_TABLE["partner_storefronts"]
        session.add(
            storefront_model(
                user_id=merchant_id,
                partner_id=merchant_id,
                name=f"Merchant Store {suffix}",
                logo_url="/uploads/site-assets/test-logo.png",
                status="active",
                is_active=True,
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        merchant_headers = await _login(client, merchant_email, merchant_password)

        blocked = await client.post(
            "/manage/products",
            headers=merchant_headers,
            json={"name": f"Merchant Luxury Blocked {suffix}", "price": 1000, "approvalStatus": "approved"},
        )
        assert blocked.status_code == 403

        created = await client.post(
            "/manage/products",
            headers=merchant_headers,
            json={"name": f"Merchant Luxury Product {suffix}", "price": 1000, "stockQuantity": 2, "isActive": True},
        )
        assert created.status_code == 201, created.text
        product = created.json()
        assert product["partner_id"] == str(merchant_id)
        assert product["approval_status"] == "pending"
        assert product["is_active"] is False
        assert product["is_featured"] is False

        product_id = product["id"]
        approve_attempt = await client.patch(
            f"/manage/products/{product_id}",
            headers=merchant_headers,
            json={"approvalStatus": "approved"},
        )
        assert approve_attempt.status_code == 403
        feature_attempt = await client.patch(
            f"/manage/products/{product_id}/featured",
            headers=merchant_headers,
            json={"isFeatured": True},
        )
        assert feature_attempt.status_code == 403
        activate_attempt = await client.patch(
            f"/manage/products/{product_id}/active",
            headers=merchant_headers,
            json={"isActive": True},
        )
        assert activate_attempt.status_code == 403


async def test_variant_upsert_requires_variant_to_belong_to_url_product() -> None:
    get_settings().require_test_fixtures_enabled("product merchant remediation tests")
    suffix = uuid.uuid4().hex[:8]
    merchant_email = f"variant-owner-{suffix}@example.com"
    merchant_id, merchant_password = await _seed_user(merchant_email, "partner")
    async with SessionFactory() as session:
        product_a = Product(name=f"Variant Owner A {suffix}", price=10, partner_id=merchant_id, approval_status="pending", is_active=False)
        product_b = Product(name=f"Variant Owner B {suffix}", price=10, partner_id=merchant_id, approval_status="pending", is_active=False)
        session.add_all([product_a, product_b])
        await session.flush()
        variant_b = ProductVariant(product_id=product_b.id, sku=f"VARB{suffix}".upper(), stock_quantity=3)
        session.add(variant_b)
        await session.commit()
        product_a_id = product_a.id
        product_b_id = product_b.id
        variant_b_id = variant_b.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        merchant_headers = await _login(client, merchant_email, merchant_password)
        cross_product_update = await client.post(
            f"/manage/products/{product_a_id}/variants",
            headers=merchant_headers,
            json={"id": str(variant_b_id), "stockQuantity": 9},
        )
        assert cross_product_update.status_code == 404

        move_attempt = await client.post(
            f"/manage/products/{product_a_id}/variants",
            headers=merchant_headers,
            json={"productId": str(product_b_id), "stockQuantity": 1},
        )
        assert move_attempt.status_code == 403


async def test_product_validation_and_duplicate_sku_error_mapping() -> None:
    get_settings().require_test_fixtures_enabled("product merchant remediation tests")
    suffix = uuid.uuid4().hex[:8].upper()
    admin_email = f"admin-remediation-{suffix.lower()}@example.com"
    _, admin_password = await _seed_user(admin_email, "admin")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_headers = await _login(client, admin_email, admin_password)

        invalid_sku = await client.post(
            "/api/admin/products",
            headers=admin_headers,
            json={"name": f"Invalid Sku {suffix}", "sku": "bad sku", "price": 100},
        )
        assert invalid_sku.status_code == 422
        assert invalid_sku.json()["detail"]["code"] == "invalid_sku"

        invalid_price = await client.post(
            "/api/admin/products",
            headers=admin_headers,
            json={"name": f"Invalid Price {suffix}", "sku": f"SKUINV{suffix}", "price": 100, "originalPrice": 90},
        )
        assert invalid_price.status_code == 422
        assert invalid_price.json()["detail"]["code"] == "invalid_original_price"

        sku = f"SKUDUP{suffix}"
        created = await client.post(
            "/api/admin/products",
            headers=admin_headers,
            json={"name": f"Duplicate SKU Base {suffix}", "sku": sku, "price": 100},
        )
        assert created.status_code == 201, created.text

        duplicate = await client.post(
            "/api/admin/products",
            headers=admin_headers,
            json={"name": f"Duplicate SKU Replay {suffix}", "sku": sku.lower(), "price": 120},
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["detail"]["code"] == "duplicate_sku"
