from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, Product, Profile, RefreshToken, User, UserRole
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


async def _count(table: str) -> int:
    async with SessionFactory() as session:
        model = MODEL_BY_TABLE[table]
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed_user(email: str, role: str, full_name: str, *, test_run_id: str | None = None) -> tuple[uuid.UUID, str]:
    password = "ValidPass123"
    async with SessionFactory() as session:
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        await session.flush()
        extra_data = {}
        if test_run_id:
            extra_data = {
                "test_run_id": test_run_id,
                "created_by_test": True,
                "test_suite_name": "pytest_full_system_postgres",
            }
        session.add(Profile(id=user.id, user_id=user.id, email=email, full_name=full_name, extra_data=extra_data))
        session.add(UserRole(user_id=user.id, role=role))
        await session.commit()
        return user.id, password


async def _login(client: AsyncClient, email: str, password: str) -> dict:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def test_live_admin_login_refresh_logout_are_persisted_in_postgres() -> None:
    test_run_id = f"pytest-admin-login-{uuid.uuid4().hex}"
    email = f"e2e-admin-login-{uuid.uuid4().hex[:10]}@example.com"
    user_id, password = await _seed_user(email, "admin", "E2E Admin Login", test_run_id=test_run_id)
    before_attempts = await _count("login_attempts")
    before_tokens = await _count("refresh_tokens")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post("/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            auth = login.json()
            assert "admin" in set(auth["roles"])

            me = await client.get("/me", headers=_headers(auth["access_token"]))
            assert me.status_code == 200
            assert me.json()["user"]["email"].lower() == email.lower()

            expired = jwt.encode(
                {
                    "sub": auth["user"]["id"],
                    "roles": auth["roles"],
                    "type": "access",
                    "iat": datetime.now(timezone.utc) - timedelta(hours=2),
                    "exp": datetime.now(timezone.utc) - timedelta(hours=1),
                },
                get_settings().jwt_secret,
                algorithm="HS256",
            )
            rejected = await client.get("/me", headers=_headers(expired))
            assert rejected.status_code == 401

            refreshed = await client.post("/auth/refresh", json={"refreshToken": auth["refresh_token"]})
            assert refreshed.status_code == 200
            fresh_auth = refreshed.json()
            logout = await client.post("/auth/logout", json={"refreshToken": fresh_auth["refresh_token"]})
            assert logout.status_code == 200

        assert await _count("login_attempts") == before_attempts + 1
        assert await _count("refresh_tokens") >= before_tokens + 2
        async with SessionFactory() as session:
            user = (await session.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one()
            assert user.last_login_at is not None
            revoked = (
                await session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_not(None))
                    .order_by(RefreshToken.updated_at.desc())
                )
            ).scalars().first()
            assert revoked is not None
    finally:
        async with SessionFactory() as session:
            await session.execute(delete(MODEL_BY_TABLE["login_attempts"]).where(MODEL_BY_TABLE["login_attempts"].email == email))
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
            await session.commit()


async def test_full_role_flow_writes_api_results_to_postgres() -> None:
    suffix = uuid.uuid4().hex[:10]
    admin_id, admin_password = await _seed_user(f"e2e-admin-{suffix}@example.com", "admin", "E2E Admin")
    delivery_id, delivery_password = await _seed_user(f"e2e-delivery-{suffix}@example.com", "delivery", "E2E Courier")
    marketer_id, marketer_password = await _seed_user(f"e2e-marketer-{suffix}@example.com", "marketer", "E2E Marketer")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "postgresql"

        admin = await _login(client, f"e2e-admin-{suffix}@example.com", admin_password)
        admin_headers = _headers(admin["access_token"])
        blocked = await client.get("/admin/sections/suppliers/records")
        assert blocked.status_code in {401, 403}

        upload = await client.post(
            "/storage/upload",
            headers=admin_headers,
            data={"purpose": "site_asset"},
            files={"file": (f"e2e-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert upload.status_code == 201, upload.text
        uploaded_url = upload.json()["url"]
        assert "/uploads/site-assets/" in uploaded_url
        assert Path(get_settings().resolved_upload_dir / uploaded_url.split("/uploads/", 1)[1]).exists()

        category = await client.post(
            "/admin/sections/categories/records",
            headers=admin_headers,
            json={"name": f"E2E Category {suffix}", "slug": f"e2e-category-{suffix}", "is_active": True},
        )
        assert category.status_code == 200, category.text
        category_id = category.json()["id"]
        brand = await client.post(
            "/admin/sections/brands/records",
            headers=admin_headers,
            json={"name": f"E2E Brand {suffix}", "slug": f"e2e-brand-{suffix}", "is_active": True},
        )
        assert brand.status_code == 200, brand.text
        brand_id = brand.json()["id"]
        supplier = await client.post(
            "/admin/sections/suppliers/records",
            headers=admin_headers,
            json={
                "name": f"E2E Supplier {suffix}",
                "phone": "+967777000111",
                "email": f"supplier-{suffix}@example.com",
                "status": "active",
            },
        )
        assert supplier.status_code == 200, supplier.text
        supplier_id = supplier.json()["id"]
        supplier_update = await client.patch(
            f"/admin/sections/suppliers/records/{supplier_id}",
            headers=admin_headers,
            json={"description": "updated by full system test"},
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
            files={"file": (f"product-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert product_image.status_code == 201, product_image.text
        product = await client.post(
            "/manage/products",
            headers=admin_headers,
            json={
                "name": f"E2E Product {suffix}",
                "description": "Product created during full PostgreSQL system verification",
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
            json={"size": "L", "color": "Gold", "colorHex": "#D99A00", "stockQuantity": 6},
        )
        assert variant.status_code == 201, variant.text
        variant_id = variant.json()["id"]
        product_update = await client.patch(
            f"/manage/products/{product_id}",
            headers=admin_headers,
            json={"name": f"E2E Product Updated {suffix}", "price": 1700},
        )
        assert product_update.status_code == 200
        feature = await client.patch(f"/manage/products/{product_id}/featured", headers=admin_headers, json={"isFeatured": True})
        assert feature.status_code == 200 and feature.json()["is_featured"] is True

        customer = await client.post(
            "/auth/register-customer",
            json={
                "email": f"e2e-customer-{suffix}@example.com",
                "password": "Customer123",
                "fullName": "E2E Customer",
                "phone": "+967711000222",
                "city": "Sanaa",
            },
        )
        assert customer.status_code == 201, customer.text
        customer_registration = customer.json()
        customer_id = customer_registration["user"]["id"]
        async with SessionFactory() as session:
            customer_user = await session.get(User, uuid.UUID(customer_id))
            customer_security = await session.get(AccountSecurity, uuid.UUID(customer_id))
            assert customer_user is not None and customer_security is not None
            customer_user.is_active = True
            customer_security.account_status = "active"
            customer_security.email_verified_at = datetime.now(timezone.utc)
            await session.commit()
        customer_auth = await _login(client, f"e2e-customer-{suffix}@example.com", "Customer123")
        customer_headers = _headers(customer_auth["access_token"])

        avatar = await client.post(
            "/me/avatar",
            headers=customer_headers,
            files={"file": (f"avatar-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert avatar.status_code == 200 and "/uploads/avatars/" in avatar.json()["avatarUrl"]

        wish = await client.post("/wishlist", headers=customer_headers, json={"productId": product_id})
        assert wish.status_code == 201, wish.text
        compare = await client.post(
            "/resources/product_comparisons/query",
            headers=customer_headers,
            json={"operation": "insert", "data": {"product_id": product_id}},
        )
        assert compare.status_code == 200, compare.text
        cart = await client.post(
            "/cart",
            headers=customer_headers,
            json={"productId": product_id, "variantId": variant_id, "quantity": 2},
        )
        assert cart.status_code == 201, cart.text
        cart_id = cart.json()["id"]
        cart_update = await client.patch(f"/cart/{cart_id}", headers=customer_headers, json={"quantity": 3})
        assert cart_update.status_code == 200

        address = await client.post(
            "/api/profile/addresses",
            headers=customer_headers,
            json={
                "label": "E2E Home",
                "recipientName": "E2E Customer",
                "phone": "+967711000222",
                "governorate": "Amanat Al Asimah",
                "city": "Sanaa",
                "address": "Verification Street",
                "latitude": 15.3694,
                "longitude": 44.1910,
                "isDefault": True,
            },
        )
        assert address.status_code == 201, address.text
        address_id = address.json()["data"]["id"]
        listed_addresses = await client.get("/api/profile/addresses", headers=customer_headers)
        assert listed_addresses.status_code == 200
        assert any(item["id"] == address_id for item in listed_addresses.json()["data"])
        updated_address = await client.patch(
            f"/api/profile/addresses/{address_id}",
            headers=customer_headers,
            json={"label": "E2E Updated Home", "city": "Sanaa"},
        )
        assert updated_address.status_code == 200
        assert updated_address.json()["data"]["label"] == "E2E Updated Home"

        coupon = await client.post(
            "/admin/sections/coupons/records",
            headers=admin_headers,
            json={"code": f"E2E{suffix.upper()}", "title": "E2E Coupon", "status": "active", "amount": 100, "is_active": True},
        )
        assert coupon.status_code == 200, coupon.text
        valid_coupon = await client.post(
            "/coupons/validate",
            headers=customer_headers,
            json={"code": f"E2E{suffix.upper()}", "subtotal": 5100},
        )
        assert valid_coupon.status_code == 200 and valid_coupon.json()["valid"] is True
        async with SessionFactory() as session:
            shipping_model = MODEL_BY_TABLE["shipping_zones"]
            shipping_zone = shipping_model(
                name=f"E2E Zone {suffix}",
                status="active",
                fee=500,
                is_active=True,
            )
            session.add(shipping_zone)
            await session.commit()
            shipping_zone_id = shipping_zone.id

        checkout_body = {
            "shippingCost": 999999,
            "shippingZoneId": str(shipping_zone_id),
            "couponCode": f"E2E{suffix.upper()}",
            "couponDiscount": 100,
            "paymentMethod": "wallet_transfer",
            "shippingAddress": {
                "recipientName": "E2E Customer",
                "phone": "+967711000222",
                "governorate": "Amanat Al Asimah",
                "city": "Sanaa",
                "address": "Verification Street",
                "shippingZoneId": str(shipping_zone_id),
            },
        }
        checkout = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"e2e-checkout-{suffix}"},
            json=checkout_body,
        )
        assert checkout.status_code == 201, checkout.text
        order = checkout.json()
        second_checkout = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"e2e-checkout-{suffix}"},
            json=checkout_body,
        )
        assert second_checkout.status_code == 200
        assert second_checkout.json()["id"] == order["id"]
        conflicting_checkout = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"e2e-checkout-{suffix}"},
            json={
                "shippingCost": 999999,
                "shippingZoneId": str(shipping_zone_id),
                "couponCode": f"E2E{suffix.upper()}",
                "couponDiscount": 100,
                "paymentMethod": "wallet_transfer",
                "shippingAddress": {
                    "recipientName": "E2E Customer",
                    "phone": "+967711000222",
                    "governorate": "Amanat Al Asimah",
                    "city": "Sanaa",
                    "address": "Different payload",
                    "shippingZoneId": str(shipping_zone_id),
                },
            },
        )
        assert conflicting_checkout.status_code == 409

        cancellation_product = await client.post(
            "/cart",
            headers=customer_headers,
            json={"productId": product_id, "variantId": variant_id, "quantity": 1},
        )
        assert cancellation_product.status_code == 201, cancellation_product.text
        cancellation_order_response = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"e2e-cancel-{suffix}"},
            json={
                "paymentMethod": "wallet_transfer",
                "shippingZoneId": str(shipping_zone_id),
                "shippingAddress": {
                    "recipientName": "E2E Customer",
                    "phone": "+967711000222",
                    "governorate": "Amanat Al Asimah",
                    "city": "Sanaa",
                    "address": "Verification Street",
                    "shippingZoneId": str(shipping_zone_id),
                },
            },
        )
        assert cancellation_order_response.status_code == 201, cancellation_order_response.text
        cancellation_order = cancellation_order_response.json()
        cancelled = await client.post(
            f"/orders/{cancellation_order['id']}/cancel",
            headers=customer_headers,
            json={"note": "E2E customer cancellation"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        duplicate_cancel = await client.post(
            f"/orders/{cancellation_order['id']}/cancel",
            headers=customer_headers,
            json={},
        )
        assert duplicate_cancel.status_code == 409

        local_request = await client.post(
            "/api/shopping/local/requests",
            headers=customer_headers,
            json={
                "product_description": "E2E local shopping request",
                "quantity": 2,
                "estimated_amount": 2500,
                "notes": "Please source this locally",
            },
        )
        assert local_request.status_code == 201, local_request.text
        local_request_id = local_request.json()["data"]["id"]
        local_list = await client.get("/api/shopping/local/requests", headers=customer_headers)
        assert local_list.status_code == 200 and any(row["id"] == local_request_id for row in local_list.json()["data"])

        international_order = await client.post(
            "/api/shopping/international/orders",
            headers=customer_headers,
            json={
                "items": [{"product_name": "E2E international item", "url": "https://example.com/item", "unit_price": 25, "quantity": 2}],
                "notes": "E2E international purchase",
            },
        )
        assert international_order.status_code == 201, international_order.text
        international_order_id = international_order.json()["data"]["id"]
        international_list = await client.get("/api/orders/international-shopping", headers=customer_headers)
        assert international_list.status_code == 200 and any(row["id"] == international_order_id for row in international_list.json()["data"])

        receipt = await client.post(
            f"/orders/{order['id']}/payment-receipt",
            headers=customer_headers,
            data={"amount": order["total"]},
            files={"file": (f"receipt-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]
        payment_review = await client.post(
            f"/payments/{receipt_id}/review",
            headers=admin_headers,
            json={"status": "approved"},
        )
        assert payment_review.status_code == 200
        status = await client.post(f"/orders/{order['id']}/status", headers=admin_headers, json={"status": "confirmed"})
        assert status.status_code == 200 and status.json()["status"] == "confirmed"

        notify = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={"userIds": [customer_id], "title": "E2E Notice", "message": "PostgreSQL notification"},
        )
        assert notify.status_code == 200 and notify.json()["sent"] == 1
        customer_notifications = await client.get("/notifications", headers=customer_headers)
        assert customer_notifications.status_code == 200 and customer_notifications.json()
        read_note = await client.patch(f"/notifications/{customer_notifications.json()[0]['id']}/read", headers=customer_headers)
        assert read_note.status_code == 200

        deleted_address = await client.delete(f"/api/profile/addresses/{address_id}", headers=customer_headers)
        assert deleted_address.status_code == 200

        ticket = await client.post(
            "/support/tickets",
            headers=customer_headers,
            json={"subject": "E2E Support", "description": "Support message"},
        )
        assert ticket.status_code == 201, ticket.text
        ticket_id = ticket.json()["id"]
        ticket_message = await client.post(
            f"/api/support/tickets/{ticket_id}/messages",
            headers=admin_headers,
            json={"message": "Admin reply"},
        )
        assert ticket_message.status_code == 201, ticket_message.text

        merchant = await client.post(
            "/auth/register-merchant",
            json={
                "email": f"e2e-merchant-{suffix}@example.com",
                "password": "Merchant123",
                "ownerName": "E2E Merchant",
                "storeName": f"E2E Store {suffix}",
                "phone": "+967733000333",
                "logoUrl": uploaded_url,
                "commercialRegisterUrl": "/uploads/partner-documents/e2e-register.pdf",
                "storeInsideImageUrl": "/uploads/partner-documents/e2e-inside.png",
                "storeOutsideImageUrl": "/uploads/partner-documents/e2e-outside.png",
            },
        )
        assert merchant.status_code == 201, merchant.text
        applications = await client.get("/admin/partner-applications", headers=admin_headers)
        app_row = next(row for row in applications.json() if row.get("email") == f"e2e-merchant-{suffix}@example.com")
        approved = await client.post("/functions/approve_partner_application", headers=admin_headers, json={"application_id": app_row["id"]})
        assert approved.status_code == 200, approved.text
        merchant_auth = await _login(client, f"e2e-merchant-{suffix}@example.com", "Merchant123")
        merchant_headers = _headers(merchant_auth["access_token"])
        partner_product = await client.post(
            "/manage/products",
            headers=merchant_headers,
            json={"name": f"E2E Partner Product {suffix}", "price": 2100, "stockQuantity": 4},
        )
        assert partner_product.status_code == 201, partner_product.text
        other_product_change = await client.patch(
            f"/manage/products/{product_id}",
            headers=merchant_headers,
            json={"name": "unauthorized change"},
        )
        assert other_product_change.status_code in {403, 404}

        delivery = await _login(client, f"e2e-delivery-{suffix}@example.com", delivery_password)
        delivery_headers = _headers(delivery["access_token"])
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        async with SessionFactory() as session:
            assignment = assignment_model(courier_id=delivery_id, user_id=delivery_id, order_id=uuid.UUID(order["id"]), status="assigned")
            session.add(assignment)
            await session.commit()
            assignment_id = str(assignment.id)
        assignments = await client.get("/delivery/assignments", headers=delivery_headers)
        assert assignments.status_code == 200 and any(row["id"] == assignment_id for row in assignments.json())
        delivery_accept = await client.patch(f"/delivery/assignments/{assignment_id}/status", headers=delivery_headers, json={"status": "accepted"})
        assert delivery_accept.status_code == 200, delivery_accept.text
        delivery_status = await client.patch(f"/delivery/assignments/{assignment_id}/status", headers=delivery_headers, json={"status": "picked_up"})
        assert delivery_status.status_code == 200, delivery_status.text
        location = await client.post(
            "/delivery/location",
            headers=delivery_headers,
            json={"assignmentId": assignment_id, "latitude": 15.3694, "longitude": 44.1910},
        )
        assert location.status_code == 200

        marketer = await _login(client, f"e2e-marketer-{suffix}@example.com", marketer_password)
        marketer_headers = _headers(marketer["access_token"])
        marketer_dashboard = await client.get("/marketer/dashboard", headers=marketer_headers)
        assert marketer_dashboard.status_code == 200
        marketer_code = await client.post(
            "/resources/public_marketer_codes/query",
            headers=marketer_headers,
            json={"operation": "insert", "data": {"code": f"MKT{suffix.upper()}", "status": "active", "is_active": True}},
        )
        assert marketer_code.status_code == 200, marketer_code.text

        report = await client.post("/reports/export", headers=admin_headers, json={"type": "orders"})
        assert report.status_code == 200, report.text
        delete_variant = await client.delete(f"/manage/product-variants/{variant_id}", headers=admin_headers)
        assert delete_variant.status_code == 200
        delete_product = await client.delete(f"/manage/products/{product_id}", headers=admin_headers)
        assert delete_product.status_code == 200
        supplier_delete = await client.post(
            "/resources/suppliers/query",
            headers=admin_headers,
            json={"operation": "delete", "filters": [{"column": "id", "operator": "eq", "value": supplier_id}]},
        )
        assert supplier_delete.status_code == 200

    async with SessionFactory() as session:
        category_model = MODEL_BY_TABLE["categories"]
        brand_model = MODEL_BY_TABLE["brands"]
        supplier_model = MODEL_BY_TABLE["suppliers"]
        variant_model = MODEL_BY_TABLE["product_variants"]
        order_model = MODEL_BY_TABLE["orders"]
        item_model = MODEL_BY_TABLE["order_items"]
        receipt_model = MODEL_BY_TABLE["payment_receipts"]
        history_model = MODEL_BY_TABLE["order_status_history"]
        audit_model = MODEL_BY_TABLE["audit_logs"]
        notification_model = MODEL_BY_TABLE["notifications"]
        comparison_model = MODEL_BY_TABLE["product_comparisons"]
        ticket_model = MODEL_BY_TABLE["support_tickets"]
        ticket_message_model = MODEL_BY_TABLE["ticket_messages"]
        storefront_model = MODEL_BY_TABLE["partner_storefronts"]
        courier_location_model = MODEL_BY_TABLE["courier_location_updates"]
        marketer_code_model = MODEL_BY_TABLE["public_marketer_codes"]
        export_model = MODEL_BY_TABLE["report_exports"]

        db_product = await session.get(Product, uuid.UUID(product_id))
        db_variant = await session.get(variant_model, uuid.UUID(variant_id))
        assert db_product is not None and db_product.deleted_at is not None and db_product.stock_quantity == 12
        assert db_variant is not None and db_variant.deleted_at is not None and db_variant.stock_quantity == 3
        assert (await session.get(category_model, uuid.UUID(category_id))).name == f"E2E Category {suffix}"
        assert (await session.get(brand_model, uuid.UUID(brand_id))).name == f"E2E Brand {suffix}"
        db_supplier = await session.get(supplier_model, uuid.UUID(supplier_id))
        assert db_supplier is not None and db_supplier.deleted_at is not None
        db_order = await session.get(order_model, uuid.UUID(order["id"]))
        assert db_order is not None and db_order.total == Decimal("5500.00")
        db_cancelled_order = await session.get(order_model, uuid.UUID(cancellation_order["id"]))
        assert db_cancelled_order is not None and db_cancelled_order.status == "cancelled"
        assert db_cancelled_order.extra_data is not None
        assert int((await session.execute(select(func.count()).select_from(item_model).where(item_model.order_id == db_order.id))).scalar_one()) == 1
        db_receipt = await session.get(receipt_model, uuid.UUID(receipt_id))
        assert db_receipt is not None and db_receipt.status == "approved"
        assert db_receipt.image_url == f"receipt:{receipt_id}"
        assert db_receipt.extra_data["storage_key"].startswith("_private/payment-receipts/")
        assert int((await session.execute(select(func.count()).select_from(history_model).where(history_model.order_id == db_order.id))).scalar_one()) >= 2
        assert int((await session.execute(select(func.count()).select_from(audit_model).where(audit_model.user_id == admin_id))).scalar_one()) >= 1
        assert int((await session.execute(select(func.count()).select_from(notification_model).where(notification_model.recipient_id == uuid.UUID(customer_id)))).scalar_one()) >= 1
        assert int((await session.execute(select(func.count()).select_from(comparison_model).where(comparison_model.user_id == uuid.UUID(customer_id)))).scalar_one()) == 1
        address_model = MODEL_BY_TABLE["customer_addresses"]
        db_address = await session.get(address_model, uuid.UUID(address_id))
        assert db_address is not None and db_address.deleted_at is not None
        assert await session.get(ticket_model, uuid.UUID(ticket_id)) is not None
        assert int((await session.execute(select(func.count()).select_from(ticket_message_model).where(ticket_message_model.ticket_id == uuid.UUID(ticket_id)))).scalar_one()) >= 2
        assert int((await session.execute(select(func.count()).select_from(storefront_model).where(storefront_model.email == f"e2e-merchant-{suffix}@example.com"))).scalar_one()) == 1
        assert int((await session.execute(select(func.count()).select_from(courier_location_model).where(courier_location_model.user_id == delivery_id))).scalar_one()) >= 1
        assert int((await session.execute(select(func.count()).select_from(marketer_code_model).where(marketer_code_model.user_id == marketer_id))).scalar_one()) == 1
        assert int((await session.execute(select(func.count()).select_from(export_model).where(export_model.user_id == admin_id))).scalar_one()) >= 1
