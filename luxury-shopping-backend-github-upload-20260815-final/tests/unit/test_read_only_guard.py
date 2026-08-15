from __future__ import annotations

import json

from backend.app.main import _read_only_request_allowed


def test_read_only_guard_allows_safe_resource_select_query() -> None:
    body = json.dumps({"operation": "select"}).encode("utf-8")

    assert _read_only_request_allowed("POST", "/resources/banners/query", body)


def test_read_only_guard_blocks_resource_mutations() -> None:
    for operation in ("insert", "update", "delete", "upsert"):
        body = json.dumps({"operation": operation}).encode("utf-8")

        assert not _read_only_request_allowed(
            "POST",
            "/resources/banners/query",
            body,
        )


def test_read_only_guard_blocks_other_post_routes() -> None:
    assert not _read_only_request_allowed("POST", "/storage/upload", b"{}")
