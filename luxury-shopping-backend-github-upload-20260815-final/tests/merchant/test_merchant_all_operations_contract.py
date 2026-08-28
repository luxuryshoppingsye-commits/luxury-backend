from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, FileAsset, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio(loop_scope="module")


PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


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
        session.add(AccountSecurity(user_id=user.id, account_status="active", security_version=0))
        await session.commit()
        return user.id, password


async def _login(client: AsyncClient, email: str, password: str) -> dict:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def _activate_user(user_id: uuid.UUID) -> None:
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        security = await session.get(AccountSecurity, user_id)
        assert user is not None and security is not None
        user.is_active = True
        security.account_status = "active"
        security.email_verified_at = datetime.now(timezone.utc)
        await session.commit()


async def test_merchant_full_operations_are_executable_in_isolated_postgres() -> None:
    suffix = uuid.uuid4().hex[:10]
    admin_email = f"merchant-e2e-admin-{suffix}@example.com"
    customer_email = f"merchant-e2e-customer-{suffix}@example.com"
    admin_id, admin_password = await _seed_user(admin_email, "admin", "Merchant E2E Admin")
    customer_id, customer_password = await _seed_user(customer_email, "customer", "Merchant E2E Customer")
    await _activate_user(customer_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_auth = await _login(client, admin_email, admin_password)
        admin_headers = _headers(admin_auth["access_token"])

        logo = await client.post(
            "/manage/product-image",
            headers=admin_headers,
            files={"file": (f"merchant-logo-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert logo.status_code == 201, logo.text
        logo_url = logo.json()["imageUrl"]
        assert logo_url.endswith(".webp")

        customer_auth = await _login(client, customer_email, customer_password)
        customer_headers = _headers(customer_auth["access_token"])
        # The storefront submits the public partnership application endpoint.
        # The old /auth/register-merchant route is an authenticated legacy
        # mutation and is intentionally not used by the customer UI.
        registration = await client.post(
            "/api/partnership/apply",
            headers=customer_headers,
            json={
                "email": customer_email,
                "businessName": f"Merchant E2E Store {suffix}",
                "businessType": "retail",
                "phone": "+967733000333",
                "description": "Merchant E2E Owner",
            },
        )
        assert registration.status_code == 201, registration.text
        application_id = registration.json()["data"]["id"]
        if not application_id:
            applications = await client.get("/admin/partner-applications", headers=admin_headers)
            assert applications.status_code == 200, applications.text
            application_id = next(row["id"] for row in applications.json() if row.get("email") == customer_email)
        approval = await client.post(
            "/functions/approve_partner_application",
            headers=admin_headers,
            json={"application_id": application_id},
        )
        assert approval.status_code == 200, approval.text
        merchant_user_id = customer_id
        await _activate_user(merchant_user_id)

        merchant_auth = await _login(client, customer_email, customer_password)
        assert "partner" in merchant_auth["roles"]
        merchant_headers = _headers(merchant_auth["access_token"])

        merchant_logo = await client.post(
            "/storage/upload",
            headers=merchant_headers,
            data={"purpose": "merchant_asset"},
            files={"file": (f"merchant-store-logo-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert merchant_logo.status_code == 201, merchant_logo.text
        merchant_logo_body = merchant_logo.json()
        assert merchant_logo_body["category"] == "merchant_asset"
        assert merchant_logo_body["url"].startswith("http://testserver/uploads/merchant-assets/")

        storefront = await client.get("/partner/storefront", headers=merchant_headers)
        assert storefront.status_code == 200, storefront.text
        saved_storefront = await client.put(
            "/partner/storefront",
            headers=merchant_headers,
            json={
                "storeName": f"Merchant E2E Store Updated {suffix}",
                "description": "Isolated merchant contract test",
                "logoUrl": merchant_logo_body["url"],
            },
        )
        assert saved_storefront.status_code == 200, saved_storefront.text
        assert saved_storefront.json()["data"]["name"].endswith(suffix)
        reloaded_storefront = await client.get("/partner/storefront", headers=merchant_headers)
        assert reloaded_storefront.status_code == 200, reloaded_storefront.text
        assert reloaded_storefront.json()["data"]["name"].endswith(suffix)
        assert reloaded_storefront.json()["data"]["logo_url"] == merchant_logo_body["url"]

        agreement = await client.get("/partner/agreement", headers=merchant_headers)
        assert agreement.status_code == 200, agreement.text
        accepted = await client.post(
            "/partner/agreement/accept",
            headers=merchant_headers,
            json={"confirmed": True, "version": agreement.json()["data"].get("version", "1.0")},
        )
        assert accepted.status_code == 200 and accepted.json()["data"]["accepted"] is True

        preferences = await client.put(
            "/partner/notification-preferences",
            headers=merchant_headers,
            json={"ordersEnabled": True, "promotionsEnabled": True, "updatesEnabled": True, "weeklyEnabled": False},
        )
        assert preferences.status_code == 200, preferences.text
        assert preferences.json()["data"]["weekly_enabled"] is False
        reloaded_preferences = await client.get(
            "/partner/notification-preferences", headers=merchant_headers
        )
        assert reloaded_preferences.status_code == 200, reloaded_preferences.text
        assert reloaded_preferences.json()["data"]["weekly_enabled"] is False

        option_ids: list[tuple[str, str]] = []
        for option in ("brands", "colors", "sizes"):
            created = await client.post(
                f"/partner/product-options/{option}",
                headers=merchant_headers,
                json={"name": f"E2E {option} {suffix}", "code": f"E2E-{option}-{suffix}"},
            )
            assert created.status_code == 201, created.text
            record_id = created.json()["data"]["id"]
            listed = await client.get(f"/partner/product-options/{option}", headers=merchant_headers)
            assert listed.status_code == 200 and any(row["id"] == record_id for row in listed.json()["data"])
            updated = await client.patch(
                f"/partner/product-options/{option}/{record_id}",
                headers=merchant_headers,
                json={"name": f"E2E {option} Updated {suffix}"},
            )
            assert updated.status_code == 200, updated.text
            option_ids.append((option, record_id))

        coupon = await client.post(
            "/partner/coupons",
            headers=merchant_headers,
            json={"code": f"MERCHANT{suffix.upper()}", "amount": 100},
        )
        assert coupon.status_code == 201, coupon.text
        coupon_id = coupon.json()["data"]["id"]
        coupon_update = await client.patch(
            f"/partner/coupons/{coupon_id}",
            headers=merchant_headers,
            json={"amount": 150},
        )
        assert coupon_update.status_code == 200, coupon_update.text

        image = await client.post(
            "/manage/product-image",
            headers=merchant_headers,
            files={"file": (f"merchant-product-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert image.status_code == 201, image.text
        image_payload = image.json()
        image_url = image_payload["imageUrl"]
        assert image_url.endswith(".webp")
        image_key = image_url.split("/uploads/", 1)[-1]
        image_path = get_settings().resolved_upload_dir / image_key
        assert image_path.is_file()

        product = await client.post(
            "/manage/products",
            headers=merchant_headers,
            json={
                "name": f"Merchant E2E Product {suffix}",
                "description": "Merchant CRUD and order test product",
                "price": 2100,
                "stockQuantity": 8,
                "imageUrl": image_url,
            },
        )
        assert product.status_code == 201, product.text
        product_id = product.json()["id"]
        assert product.json()["partner_id"] == str(merchant_user_id)
        product_update = await client.patch(
            f"/manage/products/{product_id}",
            headers=merchant_headers,
            json={"name": f"Merchant E2E Product Updated {suffix}", "price": 2200},
        )
        assert product_update.status_code == 200, product_update.text
        variant = await client.post(
            f"/manage/products/{product_id}/variants",
            headers=merchant_headers,
            json={"size": "M", "color": "Black", "colorHex": "#111111", "stockQuantity": 4},
        )
        assert variant.status_code == 201, variant.text
        variant_id = variant.json()["id"]
        variant_update = await client.post(
            f"/manage/products/{product_id}/variants",
            headers=merchant_headers,
            json={"id": variant_id, "stockQuantity": 5},
        )
        assert variant_update.status_code == 200, variant_update.text

        approved_product = await client.patch(
            f"/api/admin/products/{product_id}/approval",
            headers=admin_headers,
            json={"status": "approved"},
        )
        assert approved_product.status_code == 200, approved_product.text

        request_created = await client.post(
            "/api/partner/requests",
            headers=merchant_headers,
            json={"title": f"Merchant Request {suffix}", "description": "Special order test", "estimated_value": 5000},
        )
        assert request_created.status_code == 200, request_created.text
        request_list = await client.get("/api/partner/requests", headers=merchant_headers)
        assert request_list.status_code == 200 and request_list.json()["data"]

        shipping_model = MODEL_BY_TABLE["shipping_zones"]
        async with SessionFactory() as session:
            shipping_zone = shipping_model(name=f"Merchant E2E Zone {suffix}", status="active", fee=300, is_active=True)
            session.add(shipping_zone)
            await session.commit()
            shipping_zone_id = str(shipping_zone.id)

        customer_auth = await _login(client, customer_email, customer_password)
        customer_headers = _headers(customer_auth["access_token"])
        cart = await client.post(
            "/cart",
            headers=customer_headers,
            json={"productId": product_id, "variantId": variant_id, "quantity": 1},
        )
        assert cart.status_code == 201, cart.text
        checkout_body = {
            "paymentMethod": "wallet_transfer",
            "shippingZoneId": shipping_zone_id,
            "shippingAddress": {
                "recipientName": "Merchant E2E Customer",
                "phone": "+967711000222",
                "governorate": "Amanat Al Asimah",
                "city": "Sanaa",
                "address": "Merchant E2E Street",
                "shippingZoneId": shipping_zone_id,
            },
        }
        checkout = await client.post(
            "/orders/checkout",
            headers={**customer_headers, "Idempotency-Key": f"merchant-checkout-{suffix}"},
            json=checkout_body,
        )
        assert checkout.status_code == 201, checkout.text
        order_id = checkout.json()["id"]

        partner_orders = await client.get("/api/partner/orders", headers=merchant_headers)
        assert partner_orders.status_code == 200 and any(row["id"] == order_id for row in partner_orders.json()["data"])
        detail = await client.get(f"/api/partner/orders/{order_id}", headers=merchant_headers)
        assert detail.status_code == 200, detail.text
        for next_status in ("confirmed", "preparing", "ready_for_shipment"):
            changed = await client.patch(
                f"/api/partner/orders/{order_id}/status",
                headers=merchant_headers,
                json={"nextStatus": next_status},
            )
            assert changed.status_code == 200, changed.text

        receipt = await client.post(
            f"/orders/{order_id}/payment-receipt",
            headers=customer_headers,
            data={"paymentMethod": "wallet_transfer", "amount": "2500"},
            files={"file": (f"receipt-{suffix}.png", PNG_1X1_BYTES, "image/png")},
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json().get("id") or receipt.json().get("data", {}).get("id")
        assert receipt_id
        reviewed = await client.post(
            f"/payments/{receipt_id}/review",
            headers=admin_headers,
            json={"status": "approved", "note": "Merchant E2E receipt"},
        )
        assert reviewed.status_code == 200, reviewed.text

        refund = await client.post(
            f"/orders/{order_id}/refund",
            headers={**admin_headers, "Idempotency-Key": f"merchant-refund-{suffix}"},
            json={"amount": "2500", "reason": "Merchant E2E refund"},
        )
        assert refund.status_code == 201, refund.text
        refund_id = refund.json().get("id") or refund.json().get("data", {}).get("id")
        assert refund_id
        refund_completed = await client.patch(
            f"/api/operations/refunds/{refund_id}/status",
            headers=admin_headers,
            json={"status": "manual_completed", "completionEvidence": f"MERCHANT-E2E-{suffix}"},
        )
        assert refund_completed.status_code == 200, refund_completed.text
        refund_payload = refund_completed.json().get("data", refund_completed.json())
        assert refund_payload.get("order_payment_status") in {"refunded", "partially_refunded"}

        notification = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={
                "userIds": [str(merchant_user_id)],
                "title": "Merchant E2E notification",
                "message": "Merchant notification database delivery",
                "type": "order_status",
            },
        )
        assert notification.status_code == 200 and notification.json()["sent"] == 1
        merchant_notifications = await client.get("/notifications", headers=merchant_headers)
        assert merchant_notifications.status_code == 200
        notification_body = merchant_notifications.json()
        notification_rows = notification_body.get("data", notification_body) if isinstance(notification_body, dict) else notification_body
        assert any(row.get("title") == "Merchant E2E notification" for row in notification_rows)

        report = await client.get("/partner/reports/summary", headers=merchant_headers)
        assert report.status_code == 200, report.text

        for option, record_id in option_ids:
            deleted = await client.delete(f"/partner/product-options/{option}/{record_id}", headers=merchant_headers)
            assert deleted.status_code == 200, deleted.text
        deleted_coupon = await client.delete(f"/partner/coupons/{coupon_id}", headers=merchant_headers)
        assert deleted_coupon.status_code == 200, deleted_coupon.text

        deleted_product = await client.delete(f"/manage/products/{product_id}", headers=merchant_headers)
        assert deleted_product.status_code == 200, deleted_product.text
        assert deleted_product.json()["removed_assets"] >= 1
        assert not image_path.exists()
        async with SessionFactory() as session:
            asset = (
                await session.execute(
                    select(FileAsset).where(FileAsset.storage_key == image_key).order_by(FileAsset.created_at.desc())
                )
            ).scalars().first()
            assert asset is not None and asset.status == "deleted" and asset.deleted_at is not None
