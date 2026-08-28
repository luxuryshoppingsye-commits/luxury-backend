from __future__ import annotations

import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models.domain import AccountSecurity, Category, Product, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


def _assert_safe_database() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        pytest.fail("Refusing category integrity tests outside APP_ENV=test", pytrace=False)
    if not settings.allow_test_fixtures:
        pytest.fail("Refusing category integrity tests when ALLOW_TEST_FIXTURES is not true", pytrace=False)
    if not settings.database_is_test:
        pytest.fail("Refusing category integrity tests outside a trusted test database", pytrace=False)
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        pytest.fail("Refusing category integrity tests outside 127.0.0.1:55433", pytrace=False)
    if settings.database_name == "luxury_official_recovery":
        pytest.fail("Refusing category integrity tests on recovery database", pytrace=False)


async def _seed_admin(run_id: str) -> tuple[User, str]:
    password = "ValidPass123"
    email = f"{run_id}-admin-{uuid.uuid4().hex[:8]}@example.com"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=f"{run_id} admin"))
        session.add(UserRole(user_id=user.id, role="admin"))
        session.add(AccountSecurity(user_id=user.id, account_status="active"))
        await session.commit()
        return user, password


async def _login(client: AsyncClient, user: User, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _active_category_name_count(name: str) -> int:
    async with SessionFactory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Category)
                    .where(
                        Category.deleted_at.is_(None),
                        func.lower(func.btrim(Category.name)) == name.casefold(),
                    )
                )
            ).scalar_one()
        )


async def test_category_create_rejects_duplicate_name_and_slug_across_admin_routes() -> None:
    _assert_safe_database()
    run_id = f"cat-int-{uuid.uuid4().hex[:8]}"
    admin, password = await _seed_admin(run_id)
    name = f"Category Integrity {run_id}"
    slug = f"category-integrity-{run_id}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, password)
        created = await client.post(
            "/api/catalog/admin/categories",
            headers=headers,
            json={"name": name, "name_en": name, "slug": slug, "is_active": True},
        )
        duplicate_name = await client.post(
            "/api/catalog/admin/categories",
            headers=headers,
            json={"name": f"  {name}  ", "name_en": f"{name} alt", "slug": f"{slug}-alt", "is_active": True},
        )
        duplicate_name_en = await client.post(
            "/api/catalog/admin/categories",
            headers=headers,
            json={"name": f"{name} English duplicate", "name_en": name.upper(), "slug": f"{slug}-en-alt", "is_active": True},
        )
        duplicate_slug = await client.post(
            "/admin/sections/categories/records",
            headers=headers,
            json={"name": f"{name} slug path", "name_en": f"{name} slug path", "slug": slug.upper(), "is_active": True},
        )

    assert created.status_code == 201, created.text
    assert duplicate_name.status_code == 409, duplicate_name.text
    assert "duplicate_category_name" in duplicate_name.text
    assert duplicate_name_en.status_code == 409, duplicate_name_en.text
    assert "duplicate_category_name_en" in duplicate_name_en.text
    assert duplicate_slug.status_code == 409, duplicate_slug.text
    assert "duplicate_category_slug" in duplicate_slug.text
    assert await _active_category_name_count(name) == 1


async def test_category_update_cannot_duplicate_existing_name() -> None:
    _assert_safe_database()
    run_id = f"cat-upd-{uuid.uuid4().hex[:8]}"
    admin, password = await _seed_admin(run_id)
    first_name = f"Category First {run_id}"
    second_name = f"Category Second {run_id}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, password)
        first = await client.post(
            "/admin/sections/categories/records",
            headers=headers,
            json={"name": first_name, "name_en": first_name, "slug": f"{run_id}-first", "is_active": True},
        )
        second = await client.post(
            "/admin/sections/categories/records",
            headers=headers,
            json={"name": second_name, "name_en": second_name, "slug": f"{run_id}-second", "is_active": True},
        )
        conflict = await client.patch(
            f"/admin/sections/categories/records/{second.json()['id']}",
            headers=headers,
            json={"name": f" {first_name} "},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert conflict.status_code == 409, conflict.text
    assert "duplicate_category_name" in conflict.text
    assert await _active_category_name_count(first_name) == 1
    assert await _active_category_name_count(second_name) == 1


async def test_category_delete_is_soft_and_keeps_product_relationship() -> None:
    _assert_safe_database()
    run_id = f"cat-del-{uuid.uuid4().hex[:8]}"
    admin, password = await _seed_admin(run_id)
    category_name = f"Category Delete {run_id}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, password)
        created = await client.post(
            "/api/catalog/admin/categories",
            headers=headers,
            json={"name": category_name, "name_en": category_name, "slug": f"{run_id}-delete", "is_active": True},
        )
        assert created.status_code == 201, created.text
        category_id = uuid.UUID(created.json()["data"]["id"])

        async with SessionFactory() as session:
            product = Product(
                name=f"{run_id} product",
                sku=f"{run_id}-{uuid.uuid4().hex[:8]}",
                category_id=category_id,
                price=Decimal("10.00"),
                stock_quantity=3,
                is_active=True,
                approval_status="approved",
            )
            session.add(product)
            await session.commit()
            product_id = product.id

        deleted = await client.delete(f"/api/catalog/admin/categories/{category_id}", headers=headers)

    assert deleted.status_code == 200, deleted.text
    async with SessionFactory() as session:
        category = await session.get(Category, category_id)
        product = await session.get(Product, product_id)
    assert category is not None
    assert category.deleted_at is not None
    assert category.is_active is False
    assert product is not None
    assert product.category_id == category_id


async def test_resource_categories_query_uses_same_duplicate_guards() -> None:
    _assert_safe_database()
    run_id = f"cat-res-{uuid.uuid4().hex[:8]}"
    admin, password = await _seed_admin(run_id)
    first_name = f"Category Resource First {run_id}"
    second_name = f"Category Resource Second {run_id}"
    first_slug = f"{run_id}-resource-first"
    second_slug = f"{run_id}-resource-second"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        headers = await _login(client, admin, password)
        first = await client.post(
            "/resources/categories/query",
            headers=headers,
            json={
                "operation": "insert",
                "data": {"name": first_name, "name_en": first_name, "slug": first_slug, "is_active": True},
            },
        )
        second = await client.post(
            "/resources/categories/query",
            headers=headers,
            json={
                "operation": "insert",
                "data": {"name": second_name, "name_en": second_name, "slug": second_slug, "is_active": True},
            },
        )
        duplicate_name = await client.post(
            "/resources/categories/query",
            headers=headers,
            json={
                "operation": "insert",
                "data": {
                    "name": f"  {first_name}  ",
                    "name_en": f"{first_name} duplicate",
                    "slug": f"{first_slug}-duplicate",
                    "is_active": True,
                },
            },
        )
        duplicate_slug = await client.post(
            "/resources/categories/query",
            headers=headers,
            json={
                "operation": "insert",
                "data": {
                    "name": f"{run_id} slug duplicate",
                    "name_en": f"{run_id} slug duplicate",
                    "slug": first_slug.upper(),
                    "is_active": True,
                },
            },
        )
        second_id = second.json()[0]["id"]
        update_conflict = await client.post(
            "/resources/categories/query",
            headers=headers,
            json={
                "operation": "update",
                "filters": [{"column": "id", "operator": "eq", "value": second_id}],
                "data": {"name": f" {first_name} "},
            },
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert duplicate_name.status_code == 409, duplicate_name.text
    assert "duplicate_category_name" in duplicate_name.text
    assert duplicate_slug.status_code == 409, duplicate_slug.text
    assert "duplicate_category_slug" in duplicate_slug.text
    assert update_conflict.status_code == 409, update_conflict.text
    assert "duplicate_category_name" in update_conflict.text
    assert await _active_category_name_count(first_name) == 1
    assert await _active_category_name_count(second_name) == 1
