from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.app.database import SessionFactory
from backend.app.config import get_settings
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import AccountSecurity, LoginAttempt, Product, Profile, User, UserRole
from backend.app.security.passwords import hash_password


pytestmark = pytest.mark.asyncio


PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(payload: dict, key: str) -> str:
    value = payload.get(key) or payload.get("session", {}).get(key)
    assert value, f"{key} missing from auth response"
    return str(value)


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


async def test_postgresql_api_end_to_end() -> None:
    suffix = uuid.uuid4().hex[:10]
    async with SessionFactory() as session:
        await session.execute(delete(LoginAttempt))
        await session.commit()
    admin_email = f"admin-{suffix}@example.com"
    partner_email = f"partner-{suffix}@example.com"
    delivery_email = f"delivery-{suffix}@example.com"
    marketer_email = f"marketer-{suffix}@example.com"
    admin_id, admin_password = await _seed_user(admin_email, "admin", "Test Admin")
    partner_id, partner_password = await _seed_user(partner_email, "partner", "Test Partner")
    delivery_id, delivery_password = await _seed_user(delivery_email, "delivery", "Test Courier")
    marketer_id, marketer_password = await _seed_user(marketer_email, "marketer", "Test Marketer")

    async with SessionFactory() as session:
        category_model = MODEL_BY_TABLE["categories"]
        category = category_model(name=f"Category {suffix}", slug=f"category-{suffix}", is_active=True)
        session.add(category)
        product_id = uuid.uuid4()
        image_relative_path = f"products/{product_id}-primary.png"
        image_path = get_settings().resolved_upload_dir / image_relative_path
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(PNG_1X1_BYTES)
        product = Product(
            id=product_id,
            name=f"Product {suffix}",
            sku=f"SKU-{suffix}",
            price=1000,
            stock_quantity=20,
            is_active=True,
            approval_status="approved",
            partner_id=partner_id,
            image_url=f"/uploads/{image_relative_path}",
            images=[f"/uploads/{image_relative_path}"],
        )
        session.add(product)
        coupon_model = MODEL_BY_TABLE["coupons"]
        coupon = coupon_model(
            code=f"SAVE{suffix.upper()}",
            title="Test coupon",
            status="active",
            amount=100,
            is_active=True,
            extra_data={"uses_per_user": 3, "discount_type": "fixed", "discount_value": 100},
        )
        session.add(coupon)
        courier_model = MODEL_BY_TABLE["couriers"]
        session.add(courier_model(user_id=delivery_id, name="Test Courier", phone="+967777777777", status="active"))
        marketer_model = MODEL_BY_TABLE["marketers"]
        session.add(marketer_model(user_id=marketer_id, name="Test Marketer", phone="+967733333333", status="active"))
        shipping_model = MODEL_BY_TABLE["shipping_zones"]
        shipping_zone = shipping_model(name="Sanaa", status="active", fee=500, is_active=True)
        session.add(shipping_zone)
        await session.commit()
        product_id = product.id
        shipping_zone_id = shipping_zone.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "postgresql"

        customer_email = f"customer-{suffix}@example.com"
        customer_password = "Customer123"
        register = await client.post(
            "/auth/register-customer",
            json={
                "email": customer_email,
                "password": customer_password,
                "fullName": "Test Customer",
                "phone": "+967711111111",
                "city": "Sanaa",
                "captchaToken": "test-captcha-ok",
            },
        )
        assert register.status_code == 201, register.text
        customer_auth = register.json()
        customer_id = uuid.UUID(customer_auth["user"]["id"])
        async with SessionFactory() as session:
            user = await session.get(User, customer_id)
            account = await session.get(AccountSecurity, customer_id)
            assert user is not None and account is not None
            user.is_active = True
            account.account_status = "active"
            await session.commit()
        customer_auth = await _login(client, customer_email, customer_password)
        customer_headers = _headers(_token(customer_auth, "access_token"))

        me = await client.get("/me", headers=customer_headers)
        assert me.status_code == 200 and me.json()["user"]["id"] == str(customer_id)
        profile = await client.patch("/me", headers=customer_headers, json={"full_name": "Updated Customer"})
        assert profile.status_code == 200
        refresh = await client.post("/auth/refresh", json={"refreshToken": _token(customer_auth, "refresh_token")})
        assert refresh.status_code == 200
        customer_auth = refresh.json()
        customer_headers = _headers(_token(customer_auth, "access_token"))

        forbidden = await client.get("/admin/sections/warehouses/records", headers=customer_headers)
        assert forbidden.status_code == 403
        catalog = await client.get("/products", params={"limit": 50})
        assert catalog.status_code == 200
        assert any(row["id"] == str(product_id) for row in catalog.json())

        wishlist = await client.post("/wishlist", headers=customer_headers, json={"productId": str(product_id)})
        assert wishlist.status_code == 201
        assert (await client.get("/wishlist", headers=customer_headers)).status_code == 200
        assert (await client.delete(f"/wishlist/{product_id}", headers=customer_headers)).status_code == 204

        like = await client.put(
            f"/api/engagement/products/{product_id}/like",
            headers=customer_headers,
            json={"liked": True},
        )
        assert like.status_code == 200 and like.json()["liked"] is True
        liked_products = await client.get("/api/engagement/liked-products", headers=customer_headers)
        assert liked_products.status_code == 200
        liked_row = next(row for row in liked_products.json()["data"] if row["product_id"] == str(product_id))
        assert liked_row["products"]["id"] == str(product_id)
        assert liked_row["products"]["name"] == product.name
        unliked = await client.put(
            f"/api/engagement/products/{product_id}/like",
            headers=customer_headers,
            json={"liked": False},
        )
        assert unliked.status_code == 200 and unliked.json()["liked"] is False

        cart = await client.post(
            "/cart",
            headers=customer_headers,
            json={"productId": str(product_id), "quantity": 2},
        )
        assert cart.status_code == 201, cart.text
        cart_id = cart.json()["id"]
        assert (await client.patch(f"/cart/{cart_id}", headers=customer_headers, json={"quantity": 3})).status_code == 200
        assert (await client.delete(f"/cart/{cart_id}", headers=customer_headers)).status_code == 200
        assert (
            await client.post("/cart", headers=customer_headers, json={"productId": str(product_id), "quantity": 2})
        ).status_code == 201

        coupon = await client.post(
            "/coupons/validate",
            headers=customer_headers,
            json={"code": f"SAVE{suffix.upper()}", "subtotal": 2000},
        )
        assert coupon.status_code == 200 and coupon.json()["valid"] is True
        rpc_coupon = await client.post(
            "/functions/validate_coupon_for_checkout",
            headers=customer_headers,
            json={"p_code": f"SAVE{suffix.upper()}", "p_subtotal": 2000, "p_user_id": str(admin_id)},
        )
        assert rpc_coupon.status_code == 200 and rpc_coupon.json()["valid"] is True

        checkout_headers = {**customer_headers, "Idempotency-Key": f"checkout-{suffix}"}
        checkout_body = {
            "shippingCost": 999999,
            "shippingZoneId": str(shipping_zone_id),
            "couponCode": f"SAVE{suffix.upper()}",
            "couponDiscount": 100,
            "paymentMethod": "cash",
            "shippingAddress": {
                "recipientName": "Test Customer",
                "phone": "+967711111111",
                "governorate": "Amanat Al Asimah",
                "city": "Sanaa",
                "address": "Test",
                "shippingZoneId": str(shipping_zone_id),
            },
        }
        checkout = await client.post("/orders/checkout", headers=checkout_headers, json=checkout_body)
        assert checkout.status_code == 201, checkout.text
        order = checkout.json()
        repeated = await client.post("/orders/checkout", headers=checkout_headers, json=checkout_body)
        assert repeated.status_code == 200 and repeated.json()["id"] == order["id"]
        detail = await client.get(f"/orders/{order['id']}", headers=customer_headers)
        assert detail.status_code == 200 and len(detail.json()["items"]) == 1
        assert (await client.get("/orders", headers=customer_headers)).status_code == 200
        assert (await client.get("/orders/stores", headers=customer_headers)).status_code == 200
        assert (await client.post("/shipping/quote", headers=customer_headers, json={"city": "Sanaa"})).status_code == 200

        ticket = await client.post(
            "/functions/create_order_delay_ticket",
            headers=customer_headers,
            json={"p_order_id": order["id"], "p_target": "admin", "p_message": "Order is late"},
        )
        assert ticket.status_code == 200, ticket.text
        merchant_ticket = await client.post(
            "/functions/create_order_delay_ticket",
            headers=customer_headers,
            json={
                "p_order_id": order["id"],
                "p_target": "partner",
                "p_message": "The order is delayed. Please provide a preparation update.",
            },
        )
        assert merchant_ticket.status_code == 200, merchant_ticket.text
        merchant_ticket_id = merchant_ticket.json()["id"]
        partner_auth = await _login(client, partner_email, partner_password)
        partner_headers = _headers(_token(partner_auth, "access_token"))
        partner_tickets = await client.get("/api/support/tickets", headers=partner_headers)
        assert partner_tickets.status_code == 200, partner_tickets.text
        assert any(row["id"] == merchant_ticket_id for row in partner_tickets.json()["data"])
        partner_reply = await client.post(
            f"/api/support/tickets/{merchant_ticket_id}/messages",
            headers=partner_headers,
            json={"message": "The order is being prepared and will be updated shortly."},
        )
        assert partner_reply.status_code == 201, partner_reply.text
        customer_messages = await client.get(
            f"/api/support/tickets/{merchant_ticket_id}/messages",
            headers=customer_headers,
        )
        assert customer_messages.status_code == 200, customer_messages.text
        assert any(
            row["message"] == "The order is being prepared and will be updated shortly."
            for row in customer_messages.json()["data"]
        )
        support = await client.post(
            "/support/tickets",
            headers=customer_headers,
            json={"subject": "Need help", "description": "Test ticket"},
        )
        assert support.status_code == 201

        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        avatar = await client.post(
            "/me/avatar",
            headers=customer_headers,
            files={"file": ("avatar.png", png, "image/png")},
        )
        assert avatar.status_code == 200 and "/uploads/avatars/" in avatar.json()["avatarUrl"]

        admin_auth = await _login(client, admin_email, admin_password)
        admin_headers = _headers(_token(admin_auth, "access_token"))
        status = await client.post(
            f"/orders/{order['id']}/status",
            headers=admin_headers,
            json={"status": "confirmed"},
        )
        assert status.status_code == 200 and status.json()["status"] == "confirmed"
        manual = await client.post(
            "/admin/manual-order",
            headers={**admin_headers, "Idempotency-Key": f"manual-{suffix}"},
            json={
                "customerId": str(customer_id),
                "items": [{"productId": str(product_id), "quantity": 1}],
                "paymentMethod": "cash",
                "shippingZoneId": str(shipping_zone_id),
                "shippingAddress": {
                    "recipientName": "Test Customer",
                    "phone": "+967711111111",
                    "governorate": "Amanat Al Asimah",
                    "city": "Sanaa",
                    "address": "Manual Test",
                    "shippingZoneId": str(shipping_zone_id),
                },
            },
        )
        assert manual.status_code == 200, manual.text

        managed_image = await client.post(
            "/manage/product-image",
            headers=admin_headers,
            files={"file": (f"managed-{suffix}.png", png, "image/png")},
        )
        assert managed_image.status_code == 201, managed_image.text
        managed = await client.post(
            "/manage/products",
            headers=admin_headers,
            json={
                "name": f"Managed {suffix}",
                "price": 2500,
                "stockQuantity": 8,
                "imageUrl": managed_image.json()["imageUrl"],
                "isActive": True,
            },
        )
        assert managed.status_code == 201, managed.text
        managed_id = managed.json()["id"]
        variant = await client.post(
            f"/manage/products/{managed_id}/variants",
            headers=admin_headers,
            json={"size": "M", "color": "Black", "colorHex": "#000000", "stockQuantity": 4},
        )
        assert variant.status_code == 201
        product_delete_variant = await client.post(
            f"/manage/products/{managed_id}/variants",
            headers=admin_headers,
            json={"size": "L", "color": "Gold", "colorHex": "#B47A00", "stockQuantity": 2},
        )
        assert product_delete_variant.status_code == 201
        assert (await client.patch(f"/manage/products/{managed_id}", headers=admin_headers, json={"name": f"Managed updated {suffix}"})).status_code == 200
        featured = await client.patch(f"/manage/products/{managed_id}/featured", headers=admin_headers, json={"isFeatured": True})
        assert featured.status_code == 200 and featured.json()["is_featured"] is True
        assert (await client.delete(f"/manage/product-variants/{variant.json()['id']}", headers=admin_headers)).status_code == 200
        assert (await client.delete(f"/manage/products/{managed_id}", headers=admin_headers)).status_code == 200
        async with SessionFactory() as session:
            variant_model = MODEL_BY_TABLE["product_variants"]
            linked_variant = await session.get(variant_model, uuid.UUID(product_delete_variant.json()["id"]))
            linked_product = await session.get(Product, uuid.UUID(managed_id))
            assert linked_product is not None and linked_product.deleted_at is not None
            assert linked_product.is_active is False and linked_product.is_featured is False
            assert linked_variant is not None and linked_variant.deleted_at is not None
            assert linked_variant.is_active is False

        theme = await client.put("/settings/theme", headers=admin_headers, json={"primaryColor": "#D99A00"})
        assert theme.status_code == 200
        assert (await client.get("/settings/theme")).status_code == 200
        notify = await client.post(
            "/notifications/send",
            headers=admin_headers,
            json={"userIds": [str(customer_id)], "title": "Test", "message": "Hello"},
        )
        assert notify.status_code == 200 and notify.json()["sent"] == 1
        email_process = await client.post("/email/process", headers=admin_headers)
        assert email_process.status_code == 410
        assert email_process.json()["detail"] == "manual_worker_invocation_disabled"

        # Merchant onboarding is submitted by an authenticated customer. This
        # keeps the legacy route aligned with the production account model.
        merchant_email = customer_email
        merchant_password = customer_password
        merchant_register = await client.post(
            "/auth/register-merchant",
            headers=customer_headers,
            json={
                "storeName": f"Store {suffix}",
                "logoUrl": "/uploads/site-assets/test-logo.png",
                "commercialRegisterUrl": "/uploads/partner-documents/register.pdf",
                "storeInsideImageUrl": "/uploads/partner-documents/inside.png",
                "storeOutsideImageUrl": "/uploads/partner-documents/outside.png",
                "captchaToken": "test-captcha-ok",
            },
        )
        assert merchant_register.status_code == 201, merchant_register.text
        applications = await client.get("/admin/partner-applications", headers=admin_headers)
        application = next(row for row in applications.json() if row.get("email") == merchant_email)
        approval = await client.post(
            "/functions/approve_partner_application",
            headers=admin_headers,
            json={"application_id": application["id"]},
        )
        assert approval.status_code == 200, approval.text
        async with SessionFactory() as session:
            merchant_user = (
                await session.execute(select(User).where(User.email == merchant_email))
            ).scalar_one()
            merchant_account = await session.get(AccountSecurity, merchant_user.id)
            assert merchant_account is not None
            merchant_user.is_active = True
            merchant_account.account_status = "active"
            await session.commit()
        merchant_auth = await _login(client, merchant_email, merchant_password)
        merchant_headers = _headers(_token(merchant_auth, "access_token"))
        assert "partner" in merchant_auth["roles"]
        storefront = await client.get("/partner/storefront", headers=merchant_headers)
        assert storefront.status_code == 200 and storefront.json()["data"]["logo_url"]
        partner_product = await client.post(
            "/manage/products",
            headers=merchant_headers,
            json={"name": f"Partner Product {suffix}", "price": 3000, "stockQuantity": 5},
        )
        assert partner_product.status_code == 201 and partner_product.json()["approval_status"] == "pending"
        assert (await client.get("/partner/reports/summary", headers=merchant_headers)).status_code == 200

        delivery_auth = await _login(client, delivery_email, delivery_password)
        delivery_headers = _headers(_token(delivery_auth, "access_token"))
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        async with SessionFactory() as session:
            assignment = assignment_model(
                courier_id=delivery_id,
                user_id=delivery_id,
                order_id=uuid.UUID(order["id"]),
                status="assigned",
            )
            session.add(assignment)
            await session.commit()
            assignment_id = assignment.id
        assignments = await client.get("/delivery/assignments", headers=delivery_headers)
        assert assignments.status_code == 200 and assignments.json()
        location = await client.post(
            "/delivery/location",
            headers=delivery_headers,
            json={"assignmentId": str(assignment_id), "latitude": 15.3694, "longitude": 44.1910},
        )
        assert location.status_code == 200

        marketer_auth = await _login(client, marketer_email, marketer_password)
        marketer_dashboard = await client.get("/marketer/dashboard", headers=_headers(_token(marketer_auth, "access_token")))
        assert marketer_dashboard.status_code == 200

        reset_request = await client.post("/auth/password-reset-request", json={"email": customer_email})
        assert reset_request.status_code == 200
        async with SessionFactory() as session:
            email_model = MODEL_BY_TABLE["email_outbox"]
            row = (
                await session.execute(
                    select(email_model)
                    .where(email_model.email == customer_email)
                    .order_by(email_model.created_at.desc())
                )
            ).scalars().first()
            token = parse_qs(urlparse(row.extra_data["reset_url"]).query)["token"][0]
        reset_confirm = await client.post(
            "/auth/password-reset-confirm",
            json={"token": token, "newPassword": "ResetPass123"},
        )
        assert reset_confirm.status_code == 200
        reset_login = await _login(client, customer_email, "ResetPass123")
        assert reset_login["user"]["id"] == str(customer_id)
        customer_headers = _headers(_token(reset_login, "access_token"))

        deletion = await client.post(
            "/me/account-deletion-request",
            headers=customer_headers,
            json={"reason": "Privacy test"},
        )
        assert deletion.status_code == 200

        logout = await client.post("/auth/logout", json={"refreshToken": _token(admin_auth, "refresh_token")})
        assert logout.status_code == 200

    async with SessionFactory() as session:
        product = await session.get(Product, product_id)
        assert product is not None and product.stock_quantity == 17
