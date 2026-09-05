from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.api.routes.operations import _validated_public_contact_message
from backend.app.services.api_protection import policy_for_route


def test_public_storefront_reads_are_not_protected_by_auth_policy() -> None:
    public_reads = (
        "/products",
        "/api/catalog/products",
        "/api/catalog/image-proxy/products/example.webp",
        "/api/catalog/recommendations",
        "/api/catalog/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/variants",
        "/offers",
        "/api/catalog/offers",
        "/partners",
        "/stores",
        "/api/catalog/stores",
        "/uploads/products/example.webp",
        "/api/uploads/products/example.webp",
        "/api/content/menus",
        "/api/content/site",
        "/api/content/social-links",
        "/content/site",
        "/content/menus",
        "/content/social-links",
        "/content/theme",
        "/content/settings/public/homepage",
        "/content/sections",
        "/content/pages/home",
        "/api/content/custom-elements?page=home",
        "/api/loyalty/tiers",
        "/api/payments/accounts",
        "/api/shopping/global-sites",
        "/api/shopping/local/options",
        "/api/shopping/local/partners/11afafe1-dc42-42ee-b3d1-0bd0f871655e/products",
        "/api/suppliers/counts/products",
        "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e",
        "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/stats",
        "/share/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e",
        "/share/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/image",
    )
    for path in public_reads:
        policy = policy_for_route("GET", path)
        assert policy.authentication_required is False, path
        assert policy.policy_name in {"public_read", "search"}, path

    analytics_write = policy_for_route("POST", "/api/analytics/events")
    assert analytics_write.authentication_required is False
    assert analytics_write.policy_name == "public_write"

    cart_hydrate = policy_for_route("POST", "/api/catalog/cart/hydrate")
    assert cart_hydrate.authentication_required is False
    assert cart_hydrate.policy_name == "public_read"

    partner_application = policy_for_route("POST", "/api/partnership/apply")
    assert partner_application.authentication_required is False
    assert partner_application.rate_limit_policy == "support_write"

    contact_message = policy_for_route("POST", "/api/communication/contact")
    assert contact_message.authentication_required is False
    assert contact_message.policy_name == "public_write"
    assert contact_message.rate_limit_policy == "support_write"

    loyalty_initialize = policy_for_route("POST", "/api/loyalty/initialize")
    assert loyalty_initialize.authentication_required is True
    assert loyalty_initialize.rate_limit_policy == "customer_write"

    protected_writes = (
        ("GET", "/api/analytics/events"),
        ("POST", "/api/content/menus"),
        ("POST", "/api/content/custom-elements"),
        ("PATCH", "/api/content/custom-elements/example"),
        ("PATCH", "/api/content/site/homepage"),
        ("DELETE", "/api/content/social-links/example"),
        ("POST", "/api/uploads"),
        ("GET", "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/mine"),
        ("GET", "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/eligibility"),
        ("POST", "/api/reviews/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("PATCH", "/api/reviews/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("DELETE", "/api/reviews/11afafe1-dc42-42ee-b3d1-0bd0f871655e"),
        ("PUT", "/api/engagement/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/like"),
    )
    for method, path in protected_writes:
        policy = policy_for_route(method, path)
        assert policy.authentication_required is True, (method, path)


def test_public_contact_message_reports_the_short_message_requirement() -> None:
    with pytest.raises(HTTPException) as error:
        _validated_public_contact_message(
            {
                'name': 'عميل اختبار',
                'email': 'customer@example.com',
                'subject': 'طلب',
                'message': 'قصير',
            }
        )

    assert error.value.status_code == 422
    assert error.value.detail['code'] == 'contact_message_too_short'
    assert '10 أحرف' in error.value.detail['message']
