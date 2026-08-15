from starlette.requests import Request
from starlette.responses import Response

from app.main import _apply_cache_headers


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


def test_public_catalog_without_auth_has_bounded_public_cache_headers():
    response = Response()

    _apply_cache_headers(_request("/api/catalog/products"), response)

    assert response.headers["cache-control"] == "public, max-age=300, stale-while-revalidate=600"
    assert response.headers["vary"] == "Accept-Encoding, Origin"


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


def test_public_catalog_with_non_auth_cookie_remains_cacheable():
    response = Response()

    _apply_cache_headers(
        _request(
            "/api/catalog/products",
            headers={"Cookie": "locale=ar; theme=light"},
        ),
        response,
    )

    assert response.headers["cache-control"] == "public, max-age=300, stale-while-revalidate=600"


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


def test_mutations_are_no_store_even_on_public_paths():
    response = Response()

    _apply_cache_headers(_request("/api/catalog/products", method="POST"), response)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
