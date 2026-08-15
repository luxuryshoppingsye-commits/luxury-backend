from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_web_and_flutter_use_the_single_render_backend_contract() -> None:
    required = [
        ROOT / "luxury-shopping-handover-20260609/src/lib/api/client.ts",
        ROOT / "luxury-shopping-handover-20260609/src/lib/backendOrigins.ts",
        ROOT / "lib/core/config/app_config.dart",
    ]
    if not all(path.is_file() for path in required):
        pytest.skip("Cross-platform sources are not included in the standalone backend repository")
    web_client = _read("luxury-shopping-handover-20260609/src/lib/api/client.ts")
    web_origins = _read("luxury-shopping-handover-20260609/src/lib/backendOrigins.ts")
    flutter_config = _read("lib/core/config/app_config.dart")

    assert "https://luxury-backend-34ht.onrender.com" in web_origins
    assert "wss://luxury-backend-34ht.onrender.com" in web_origins
    assert "DATABASE_URL" not in web_client
    assert "_productionApiBaseUrl = 'https://luxury-backend-34ht.onrender.com'" in flutter_config
    assert "_productionWsBaseUrl = 'wss://luxury-backend-34ht.onrender.com'" in flutter_config


def test_cart_order_and_notification_records_are_scoped_to_authenticated_uuid() -> None:
    commerce_path = ROOT / "backend/app/api/routes/commerce.py"
    notifications_path = ROOT / "backend/app/services/notification_service.py"
    if not commerce_path.is_file():
        commerce_path = BACKEND_ROOT / "app/api/routes/commerce.py"
    if not notifications_path.is_file():
        notifications_path = BACKEND_ROOT / "app/services/notification_service.py"
    commerce = commerce_path.read_text(encoding="utf-8")
    notifications = notifications_path.read_text(encoding="utf-8")

    assert "UserCart.user_id == user.id" in commerce
    assert "UserCart(user_id=user.id" in commerce
    assert "Order(\n            order_number" in commerce
    assert "user_id=user.id" in commerce
    assert "user_id=user.id, recipient_id=user.id" in commerce
    assert "user_id=payload.user_id" in notifications
    assert "model(user_id=user_id" in notifications
    assert "uuid.UUID(notification_id)" in notifications
