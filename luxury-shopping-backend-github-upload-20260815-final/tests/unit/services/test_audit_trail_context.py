from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from starlette.requests import Request

from backend.app.api.routes.operations import _activity_audit_payload
from backend.app.services.audit_trail import (
    add_audit_log,
    request_audit_metadata,
    reset_audit_request_context,
    set_audit_request_context,
    with_request_audit_metadata,
)


class _AuditSink:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row: object) -> None:
        self.rows.append(row)


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "scheme": "https",
            "path": "/api/resources/products",
            "query_string": b"",
            "headers": [
                (b"host", b"api.example.test"),
                (b"user-agent", b"Mozilla/5.0 (Linux; Android 14) Chrome/140.0.0.0 Mobile"),
            ],
            "client": ("203.0.113.10", 443),
            "server": ("api.example.test", 443),
        }
    )
    request.state.request_id = "request-audit-context-test"
    return request


def test_audit_log_captures_trusted_request_context() -> None:
    token = set_audit_request_context(_request())
    try:
        metadata = request_audit_metadata()
        assert metadata["ip_address"] == "203.0.113.10"
        assert metadata["client_label"] == "جوال · Android · Chrome"
        assert metadata["request_id"] == "request-audit-context-test"
        assert metadata["request_method"] == "PATCH"

        merged = with_request_audit_metadata({"table_name": "products", "record_id": "product-1"})
        assert merged["table_name"] == "products"
        assert merged["ip_address"] == "203.0.113.10"

        sink = _AuditSink()
        add_audit_log(
            sink,  # type: ignore[arg-type]
            user_id=uuid.uuid4(),
            action="products.update",
            description="Updated product product-1",
            extra_data={"table_name": "products", "record_id": "product-1"},
        )
        event = sink.rows[0]
        assert event.extra_data["ip_address"] == "203.0.113.10"
        assert event.extra_data["table_name"] == "products"
    finally:
        reset_audit_request_context(token)


def test_audit_activity_payload_exposes_actor_and_request_context() -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="products.update",
        description="Updated product product-1",
        created_at=datetime(2026, 9, 21, 22, 44, tzinfo=timezone.utc),
        extra_data={
            "action": "update",
            "table_name": "products",
            "record_id": "product-1",
            "ip_address": "203.0.113.10",
            "client_label": "جوال · Android · Chrome",
            "request_id": "request-audit-context-test",
            "request_path": "/api/resources/products",
            "request_method": "PATCH",
        },
    )

    payload = _activity_audit_payload(row, user_name="متجر رفاهية للتسوق", user_roles=["admin"])

    assert payload["user_name"] == "متجر رفاهية للتسوق"
    assert payload["user_roles"] == ["admin"]
    assert payload["action"] == "update"
    assert payload["table_name"] == "products"
    assert payload["ip_address"] == "203.0.113.10"
    assert payload["client_label"] == "جوال · Android · Chrome"
