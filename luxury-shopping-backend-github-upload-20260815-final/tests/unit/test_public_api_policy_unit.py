from __future__ import annotations

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
    )
    for method, path in protected_writes:
        policy = policy_for_route(method, path)
        assert policy.authentication_required is True, (method, path)
