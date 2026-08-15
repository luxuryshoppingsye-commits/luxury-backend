from __future__ import annotations

from typing import Any

from fastapi import HTTPException


TERMINAL_ORDER_STATUSES = {"delivered", "cancelled", "rejected", "refunded"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "accepted", "rejected", "cancelled"},
    "confirmed": {"accepted", "preparing", "ready_for_shipment", "cancelled"},
    "accepted": {"preparing", "ready_for_shipment", "cancelled"},
    "processing": {"preparing", "ready_for_shipment", "cancelled"},
    "preparing": {"ready_for_shipment", "shipped", "cancelled"},
    "ready_for_shipment": {"shipped", "out_for_delivery", "cancelled"},
    "shipped": {"out_for_delivery", "delivered", "failed_delivery", "cancelled"},
    "out_for_delivery": {"delivered", "failed_delivery", "postponed"},
    "postponed": {"out_for_delivery", "failed_delivery", "cancelled"},
    "failed_delivery": {"out_for_delivery", "cancelled"},
}

COURIER_ALLOWED_TRANSITIONS = {
    "ready_for_shipment": {"shipped", "out_for_delivery"},
    "shipped": {"out_for_delivery", "failed_delivery"},
    "out_for_delivery": {"delivered", "failed_delivery", "postponed"},
    "postponed": {"out_for_delivery", "failed_delivery"},
}

# توافق مع الحالات القديمة التي كانت تستخدمها لوحة الإدارة قبل توحيد آلة الحالات.
LEGACY_STATUS_ALIASES = {
    "new": "pending",
    "processed": "preparing",
    "shipping": "ready_for_shipment",
    "delivering": "out_for_delivery",
}


def normalize_status(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return LEGACY_STATUS_ALIASES.get(normalized, normalized)


def assert_allowed_transition(previous: Any, next_status: Any, *, courier: bool = False) -> tuple[str, str]:
    previous_status = normalize_status(previous or "pending")
    target_status = normalize_status(next_status)
    if not target_status:
        raise HTTPException(status_code=400, detail="order_status_required")
    if previous_status == target_status:
        raise HTTPException(status_code=409, detail="duplicate_order_status")
    if previous_status in TERMINAL_ORDER_STATUSES:
        raise HTTPException(status_code=409, detail="invalid_order_transition")
    allowed = COURIER_ALLOWED_TRANSITIONS if courier else ALLOWED_TRANSITIONS
    if target_status not in allowed.get(previous_status, set()):
        raise HTTPException(status_code=409, detail="invalid_order_transition")
    return previous_status, target_status


def assert_delivery_proof(status: str, body: dict[str, Any]) -> None:
    if normalize_status(status) != "delivered":
        return
    proof = (
        body.get("deliveryProof")
        or body.get("delivery_proof")
        or body.get("proof")
        or body.get("otp")
        or body.get("confirmationCode")
        or body.get("confirmation_code")
    )
    if not str(proof or "").strip():
        raise HTTPException(status_code=422, detail="delivery_proof_required")
