from starlette.requests import Request
from starlette.responses import Response

from app.main import _apply_cache_headers, _apply_security_headers, _should_invalidate_public_cache


def _request(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": raw_headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_public_catalog_without_auth_is_edge_cacheable():
    response = Response()

    _apply_cache_headers(_request("/api/catalog/products"), response)

    assert response.headers["cache-control"] == "public, max-age=30, stale-while-revalidate=30"
    assert "pragma" not in response.headers
    assert "expires" not in response.headers


def test_public_catalog_with_auth_is_not_persistently_cacheable():
    response = Response()

    _apply_cache_headers(
        _request("/api/catalog/products", headers={"Authorization": "Bearer redacted"}),
        response,
    )

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert "Authorization" in response.headers["vary"]


def test_public_catalog_with_non_auth_cookie_is_edge_cacheable():
    response = Response()

    _apply_cache_headers(
        _request(
            "/api/catalog/products",
            headers={"Cookie": "locale=ar; theme=light"},
        ),
        response,
    )

    assert response.headers["cache-control"] == "public, max-age=30, stale-while-revalidate=30"
    assert "pragma" not in response.headers


def test_health_is_not_cached():
    response = Response()

    _apply_cache_headers(_request("/health/ready"), response)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_public_uploads_keep_static_file_cache_headers():
    response = Response()

    _apply_cache_headers(_request("/uploads/products/example.webp"), response)

    assert response.headers["cache-control"] == "public, max-age=86400, immutable"


def test_share_images_are_cross_origin_and_cacheable():
    response = Response()

    _apply_security_headers(
        _request("/share/products/11afafe1-dc42-42ee-b3d1-0bd0f871655e/image"),
        response,
    )

    assert response.headers["cache-control"] == "public, max-age=86400, immutable"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"


def test_public_catalog_with_refresh_cookie_is_not_persistently_cacheable():
    response = Response()

    _apply_cache_headers(
        _request(
            "/api/catalog/products",
            headers={"Cookie": "rt=private-refresh-token"},
        ),
        response,
    )

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_private_customer_and_admin_paths_are_no_store():
    for path in ["/me", "/orders", "/payments/review", "/admin/products", "/resources/orders/query"]:
        response = Response()

        _apply_cache_headers(_request(path), response)

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["expires"] == "0"


def test_theme_reads_are_no_store_so_published_changes_are_immediate():
    for path in ["/content/theme", "/api/content/theme", "/settings/theme", "/api/settings/theme"]:
        response = Response()

        _apply_cache_headers(_request(path), response)

        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["pragma"] == "no-cache", path


def test_mutations_are_no_store_even_on_public_paths():
    response = Response()

    _apply_cache_headers(_request("/api/catalog/products", method="POST"), response)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_unrelated_writes_do_not_evict_public_read_cache():
    assert not _should_invalidate_public_cache(_request("/orders", method="POST"))
    assert not _should_invalidate_public_cache(_request("/api/analytics/events", method="POST"))


def test_catalog_and_content_writes_evict_public_read_cache():
    assert _should_invalidate_public_cache(_request("/manage/products/123", method="PATCH"))
    assert _should_invalidate_public_cache(_request("/api/content/site/homepage", method="PATCH"))
