from __future__ import annotations

import base64
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Product, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio(loop_scope="module")


PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)
PNG_1X1 = base64.b64encode(PNG_1X1_BYTES).decode()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(email: str, role: str, full_name: str) -> tuple[uuid.UUID, str]:
    password = "ValidPass123"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=full_name))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return user.id, password


async def _login(client: AsyncClient, email: str, password: str) -> dict:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def _count_prefixed(table_name: str, field_name: str, prefix: str) -> int:
    async with SessionFactory() as session:
        model = MODEL_BY_TABLE[table_name]
        field = getattr(model, field_name)
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(field.like(f"{prefix}%"))
                )
            ).scalar_one()
        )


async def _row_exists(table_name: str, record_id: str) -> bool:
    async with SessionFactory() as session:
        return await session.get(MODEL_BY_TABLE[table_name], uuid.UUID(record_id)) is not None


async def test_admin_core_operations_write_to_postgresql_and_enforce_roles() -> None:
    run_id = f"CODEX_ADMIN_E2E_TEST_{uuid.uuid4().hex[:10]}"
    admin_id, admin_password = await _seed_user(
        f"{run_id.lower()}_admin@example.com", "admin", f"{run_id} Admin"
    )
    customer_id, customer_password = await _seed_user(
        f"{run_id.lower()}_customer@example.com", "customer", f"{run_id} Customer"
    )

    before_categories = await _count_prefixed("categories", "name", run_id)
    before_brands = await _count_prefixed("brands", "name", run_id)
    before_suppliers = await _count_prefixed("suppliers", "name", run_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "postgresql"

        admin_auth = await _login(client, f"{run_id.lower()}_admin@example.com", admin_password)
        admin_headers = _headers(admin_auth["access_token"])
        assert "admin" in set(admin_auth["roles"])

        customer_auth = await _login(client, f"{run_id.lower()}_customer@example.com", customer_password)
        customer_headers = _headers(customer_auth["access_token"])
        forbidden = await client.get("/admin/sections/categories/records", headers=customer_headers)
        assert forbidden.status_code == 403

        categories = await client.get("/admin/sections/categories/records", headers=admin_headers)
        assert categories.status_code == 200

        category = await client.post(
            "/admin/sections/categories/records",
            headers=admin_headers,
            json={"name": f"{run_id} Category", "slug": f"{run_id.lower()}-category", "is_active": True},
        )
        assert category.status_code == 200, category.text
        category_id = category.json()["id"]
        assert await _row_exists("categories", category_id)

        brand = await client.post(
            "/admin/sections/brands/records",
            headers=admin_headers,
            json={"name": f"{run_id} Brand", "slug": f"{run_id.lower()}-brand", "is_active": True},
        )
        assert brand.status_code == 200, brand.text
        brand_id = brand.json()["id"]
        assert await _row_exists("brands", brand_id)

        supplier = await client.post(
            "/admin/sections/suppliers/records",
            headers=admin_headers,
            json={
                "name": f"{run_id} Supplier",
                "phone": "+967777000111",
                "email": f"{run_id.lower()}_supplier@example.com",
                "status": "active",
            },
        )
        assert supplier.status_code == 200, supplier.text
        supplier_id = supplier.json()["id"]
        assert await _row_exists("suppliers", supplier_id)

        supplier_update = await client.patch(
            f"/admin/sections/suppliers/records/{supplier_id}",
            headers=admin_headers,
            json={"description": f"{run_id} updated supplier"},
        )
        assert supplier_update.status_code == 200
        supplier_disable = await client.post(
            f"/admin/sections/suppliers/records/{supplier_id}/disable",
            headers=admin_headers,
        )
        assert supplier_disable.status_code == 200
        product_image = await client.post(
            "/manage/product-image",
            headers=admin_headers,
            files={"file": (f"{run_id.lower()}-product.png", PNG_1X1_BYTES, "image/png")},
        )
        assert product_image.status_code == 201, product_image.text

        product = await client.post(
            "/manage/products",
            headers=admin_headers,
            json={
                "name": f"{run_id} Product",
                "description": "Admin contract test product",
                "price": 1500,
                "stockQuantity": 12,
                "categoryId": category_id,
                "brandId": brand_id,
                "supplierId": supplier_id,
                "imageUrl": product_image.json()["imageUrl"],
                "isActive": True,
            },
        )
        assert product.status_code == 201, product.text
        product_id = product.json()["id"]

        variant = await client.post(
            f"/manage/products/{product_id}/variants",
            headers=admin_headers,
            json={"size": "M", "color": "Black", "stockQuantity": 5, "isActive": True},
        )
        assert variant.status_code == 201, variant.text
        variant_id = variant.json()["id"]

        product_update = await client.patch(
            f"/manage/products/{product_id}",
            headers=admin_headers,
            json={"name": f"{run_id} Product Updated", "price": 1750},
        )
        assert product_update.status_code == 200
        featured = await client.patch(
            f"/manage/products/{product_id}/featured",
            headers=admin_headers,
            json={"isFeatured": True},
        )
        assert featured.status_code == 200 and featured.json()["is_featured"] is True
        active = await client.patch(
            f"/manage/products/{product_id}/active",
            headers=admin_headers,
            json={"isActive": False},
        )
        assert active.status_code == 200 and active.json()["is_active"] is False

        campaign = await client.post(
            "/marketing/campaigns",
            headers=admin_headers,
            json={"title": f"{run_id} Campaign", "status": "draft", "message": "Admin E2E"},
        )
        assert campaign.status_code == 201

        notify = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={
                "userIds": [str(customer_id)],
                "title": f"{run_id} Notice",
                "message": "Admin notification test",
            },
        )
        assert notify.status_code == 200 and notify.json()["sent"] == 1

        admin_notice = await client.post(
            "/admin-notifications/send",
            headers=admin_headers,
            json={"title": f"{run_id} Admin Notice", "message": "Admin internal notification"},
        )
        assert admin_notice.status_code == 200
        notice_id = admin_notice.json()["id"]
        read_notice = await client.patch(
            f"/admin-notifications/{notice_id}/read",
            headers=admin_headers,
        )
        assert read_notice.status_code == 200 and read_notice.json()["is_read"] is True
        delete_notice = await client.delete(f"/admin-notifications/{notice_id}", headers=admin_headers)
        assert delete_notice.status_code == 200

        export = await client.post("/reports/export", headers=admin_headers, json={"type": "orders"})
        assert export.status_code == 200
        assert export.json()["status"] in {"queued", "ready"}

        backup = await client.post("/backups/create", headers=admin_headers)
        assert backup.status_code == 200, backup.text
        verify_backup = await client.get(f"/backups/{backup.json()['id']}/verify", headers=admin_headers)
        assert verify_backup.status_code == 200
        assert verify_backup.json()["ok"] is True

        assert (await client.delete(f"/manage/product-variants/{variant_id}", headers=admin_headers)).status_code == 200
        assert (await client.delete(f"/manage/products/{product_id}", headers=admin_headers)).status_code == 200

    assert await _count_prefixed("categories", "name", run_id) == before_categories + 1
    assert await _count_prefixed("brands", "name", run_id) == before_brands + 1
    assert await _count_prefixed("suppliers", "name", run_id) == before_suppliers + 1

    async with SessionFactory() as session:
        product_row = await session.get(Product, uuid.UUID(product_id))
        assert product_row is None or product_row.is_active is False

        audit_model = MODEL_BY_TABLE["audit_logs"]
        audit_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(audit_model).where(audit_model.user_id == admin_id)
                )
            ).scalar_one()
        )
        assert audit_count >= 1
