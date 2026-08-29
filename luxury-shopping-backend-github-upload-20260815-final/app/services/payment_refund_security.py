from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import FileAsset, Order, OrderItem, Product, ProductVariant, User
from ..repositories.resources import serialize_record
from ..storage import FileStorage
from .financial_calculator import (
    approved_payment_total,
    money,
    refunded_total,
    sync_order_payment_status,
)
from .image_pipeline import prepare_image_upload


FINANCE_REVIEW_ROLES = frozenset({"admin", "manager", "finance"})
RECEIPT_PENDING_STATUSES = frozenset({"pending", "pending_review", "uploaded", "reviewing"})
RECEIPT_REVIEW_STATUSES = frozenset({"approved", "rejected"})
REFUND_COMPLETED_STATUSES = frozenset({"completed", "succeeded", "provider_succeeded", "manual_completed"})
REFUND_WORKFLOW_STATUSES = frozenset(
    {
        "requires_manual_action",
        "manual_processing",
        "provider_processing",
        "failed",
        "rejected",
        *REFUND_COMPLETED_STATUSES,
    }
)
RECEIPT_FORBIDDEN_INPUT_FIELDS = frozenset(
    {"receipt_url", "receiptUrl", "image_url", "imageUrl", "proof_url", "proofUrl", "base64", "dataBase64", "external_url", "externalUrl"}
)
RECEIPT_ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"})
SIGNED_RECEIPT_MIN_AGE_SECONDS = 30
SIGNED_RECEIPT_MAX_AGE_SECONDS = 300


def require_finance_actor(roles: set[str]) -> None:
    if not roles.intersection(FINANCE_REVIEW_ROLES):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="finance_permission_required")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    if isinstance(value, Decimal):
        return str(money(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _safe_text(value: Any, *, max_len: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_audit_log(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    action: str,
    description: str,
    extra_data: dict[str, Any] | None = None,
) -> None:
    model = MODEL_BY_TABLE["audit_logs"]
    session.add(
        model(
            user_id=user_id,
            type=action,
            description=description,
            extra_data=_jsonable(extra_data or {}),
        )
    )


def _safe_receipt_response(row: Any, order: Order | None = None) -> dict[str, Any]:
    receipt_ref = f"receipt:{row.id}"
    extra = dict(getattr(row, "extra_data", {}) or {})
    payload: dict[str, Any] = {
        "id": str(row.id),
        "receipt_id": str(row.id),
        "order_id": str(row.order_id) if getattr(row, "order_id", None) else None,
        "user_id": str(row.user_id) if getattr(row, "user_id", None) else None,
        "status": str(row.status or ""),
        "amount": str(money(getattr(row, "amount", 0))),
        "receipt_url": receipt_ref,
        "receiptPath": receipt_ref,
        "payment_receipt_url": receipt_ref,
        "reviewed_by": str(row.reviewed_by) if getattr(row, "reviewed_by", None) else None,
        "reviewed_at": row.reviewed_at.isoformat() if getattr(row, "reviewed_at", None) else None,
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
        "payment_method": _safe_text(extra.get("payment_method"), max_len=80),
        "transaction_reference": _safe_text(extra.get("transaction_reference"), max_len=160),
        "source_label": _safe_text(extra.get("source_label"), max_len=160),
        "mime_type": _safe_text(extra.get("mime_type"), max_len=80),
        "size_bytes": extra.get("size_bytes"),
    }
    if order is not None:
        payload["orders"] = {
            "id": str(order.id),
            "order_number": order.order_number,
            "user_id": str(order.user_id),
            "status": order.status,
            "payment_status": order.payment_status,
            "payment_method": order.payment_method,
            "total": str(money(order.total)),
            "currency_code": order.currency_code,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }
    return payload


def _safe_refund_response(row: Any, order: Order | None = None) -> dict[str, Any]:
    extra = dict(getattr(row, "extra_data", {}) or {})
    payload = {
        "id": str(row.id),
        "order_id": str(row.order_id) if getattr(row, "order_id", None) else None,
        "user_id": str(row.user_id) if getattr(row, "user_id", None) else None,
        "status": str(row.status or ""),
        "amount": str(money(getattr(row, "amount", 0))),
        "reason": str(getattr(row, "reason", "") or ""),
        "provider_status": _safe_text(extra.get("provider_status"), max_len=80),
        "requires_manual_action": bool(extra.get("requires_manual_action")),
        "manual_completion_required": bool(extra.get("manual_completion_required")),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
    }
    if order is not None:
        payload["order_payment_status"] = order.payment_status
    return payload


async def _load_order_for_receipt(
    session: AsyncSession,
    order_id: uuid.UUID,
    user: User,
    roles: set[str],
) -> Order:
    order = (
        await session.execute(select(Order).where(Order.id == order_id, Order.deleted_at.is_(None)).with_for_update())
    ).scalar_one_or_none()
    if order is None or (order.user_id != user.id and not roles.intersection(FINANCE_REVIEW_ROLES)):
        raise HTTPException(status_code=404, detail="order_not_found")
    return order


async def _parse_required_receipt_form(request: Request) -> tuple[UploadFile, dict[str, Any]]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        if "application/json" in content_type:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and RECEIPT_FORBIDDEN_INPUT_FIELDS.intersection(body):
                raise HTTPException(status_code=422, detail="payment_proof_file_required")
        raise HTTPException(status_code=422, detail="payment_proof_file_required")

    form = await request.form()
    forbidden = RECEIPT_FORBIDDEN_INPUT_FIELDS.intersection(form.keys())
    if forbidden:
        raise HTTPException(status_code=422, detail="payment_proof_file_required")
    candidate = form.get("file") or form.get("receipt") or form.get("proof")
    if not isinstance(candidate, UploadFile) and not (hasattr(candidate, "filename") and hasattr(candidate, "read")):
        raise HTTPException(status_code=422, detail="payment_proof_file_required")
    fields = {
        key: value
        for key, value in form.items()
        if not isinstance(value, UploadFile) and not (hasattr(value, "filename") and hasattr(value, "read"))
    }
    return candidate, fields


async def create_payment_receipt(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    request: Request,
    user: User,
    roles: set[str],
    storage: FileStorage,
) -> dict[str, Any]:
    order = await _load_order_for_receipt(session, order_id, user, roles)
    upload_file, fields = await _parse_required_receipt_form(request)
    data = await upload_file.read()
    if not data:
        raise HTTPException(status_code=422, detail="payment_proof_file_required")
    amount = await receipt_amount_for_order_compatible(session, order, fields.get("amount"))
    image_metadata: dict[str, Any] = {}
    upload_name = upload_file.filename or "payment-receipt"
    upload_content_type = str(getattr(upload_file, "content_type", "") or "").lower()
    if upload_content_type.startswith("image/") or Path(upload_name).suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"
    }:
        try:
            prepared = await prepare_image_upload(
                data,
                upload_name,
                upload_content_type,
                policy_key="payment_receipt",
                max_bytes=min(10 * 1024 * 1024, get_settings().max_upload_bytes),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=415, detail="invalid_receipt_image") from exc
        data = prepared.data
        upload_name = prepared.filename
        image_metadata = {
            "image_pipeline": "webp",
            "image_width": prepared.width,
            "image_height": prepared.height,
            "original_size_bytes": prepared.original_size_bytes,
            "ai_enhanced": False,
            "ai_provider": "local_webp_receipt_safe",
        }
    stored = storage.save_bytes(
        "payment_receipt",
        upload_name,
        data,
        str(request.base_url),
        roles=roles,
    )

    model = MODEL_BY_TABLE["payment_receipts"]
    row = model(
        order_id=order.id,
        user_id=user.id,
        status="pending_review",
        image_url=None,
        amount=amount,
        extra_data={
            "storage_provider": stored.storage_provider,
            "storage_bucket": stored.storage_bucket,
            "storage_key": stored.relative_path,
            "storage_visibility": stored.visibility,
            "generated_filename": Path(stored.relative_path).name,
            "original_filename": _safe_text(upload_file.filename, max_len=240) or "payment-receipt",
            "mime_type": stored.content_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.sha256,
            "malware_scan_status": stored.scan_status,
            "malware_scan_provider": stored.scan_provider,
            "payment_method": _safe_text(fields.get("paymentMethod") or fields.get("payment_method"), max_len=80),
            "source_label": _safe_text(fields.get("sourceLabel") or fields.get("source_label"), max_len=160),
            "transaction_reference": _safe_text(fields.get("transactionReference") or fields.get("transaction_reference"), max_len=160),
            "financial_policy": "backend_decimal_half_up_2dp",
            "receipt_payload_policy": "metadata_only_no_embedded_payload_no_external_url",
            **image_metadata,
        },
    )
    session.add(row)
    await session.flush()
    asset = FileAsset(
        owner_user_id=user.id,
        created_by=user.id,
        policy_key=stored.policy_key,
        visibility=stored.visibility,
        storage_provider=stored.storage_provider,
        storage_bucket=stored.storage_bucket,
        storage_key=stored.relative_path,
        original_filename=stored.original_filename,
        content_type=stored.content_type,
        size_bytes=stored.size,
        checksum_sha256=stored.sha256,
        status="available",
        scan_status=stored.scan_status,
        scan_provider=stored.scan_provider,
        entity_type="payment_receipt",
        entity_id=row.id,
        extra_data={
            "payment_receipt_id": str(row.id),
            "order_id": str(order.id),
            "quarantine_path": stored.quarantine_path,
            **image_metadata,
        },
    )
    session.add(asset)
    await session.flush()
    extra_data = dict(row.extra_data or {})
    extra_data["file_asset_id"] = str(asset.id)
    row.extra_data = extra_data
    row.image_url = f"receipt:{row.id}"
    _add_audit_log(
        session,
        user_id=user.id,
        action="payment_receipt.uploaded",
        description=f"Uploaded payment receipt for order {order.order_number}",
        extra_data={"order_id": order.id, "payment_receipt_id": row.id, "size_bytes": stored.size, "mime_type": stored.content_type},
    )
    await session.flush()
    await session.refresh(row)
    payload = _safe_receipt_response(row, order)
    await session.commit()
    return payload


async def receipt_amount_for_order_compatible(session: AsyncSession, order: Order, raw_amount: Any) -> Decimal:
    from .financial_calculator import receipt_amount_for_order

    return await receipt_amount_for_order(session, order, raw_amount)


async def list_payment_receipts_for_review(session: AsyncSession, *, limit: int = 500) -> list[dict[str, Any]]:
    model = MODEL_BY_TABLE["payment_receipts"]
    statement = select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(limit)
    rows = list((await session.execute(statement)).scalars())
    order_ids = [row.order_id for row in rows if row.order_id]
    orders_by_id: dict[uuid.UUID, Order] = {}
    if order_ids:
        order_rows = (await session.execute(select(Order).where(Order.id.in_(order_ids)))).scalars()
        orders_by_id = {order.id: order for order in order_rows}
    return [_safe_receipt_response(row, orders_by_id.get(row.order_id)) for row in rows]


async def review_payment_receipt(
    session: AsyncSession,
    *,
    payment_id: uuid.UUID,
    body: dict[str, Any],
    staff: User,
    roles: set[str],
) -> dict[str, Any]:
    require_finance_actor(roles)
    if "approved" in body and "status" not in body:
        next_status = "approved" if bool(body.get("approved")) else "rejected"
    else:
        next_status = str(body.get("status") or "").strip().lower()
    if next_status not in RECEIPT_REVIEW_STATUSES:
        raise HTTPException(status_code=422, detail="invalid_payment_receipt_status")
    reason = _safe_text(body.get("reason") or body.get("rejection_reason"), max_len=500)

    model = MODEL_BY_TABLE["payment_receipts"]
    row = (
        await session.execute(select(model).where(model.id == payment_id, model.deleted_at.is_(None)).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    current_status = str(row.status or "pending_review")
    if current_status not in RECEIPT_PENDING_STATUSES:
        if current_status == next_status:
            order = await session.get(Order, row.order_id) if row.order_id else None
            payload = _safe_receipt_response(row, order)
            payload["idempotency_replayed"] = True
            return payload
        raise HTTPException(status_code=409, detail="payment_receipt_already_reviewed")

    # Resolve the current state before validating decision-specific fields.
    # A repeated decision must remain an idempotent replay/conflict even when
    # the repeated request omits fields that only apply to a new decision.
    if next_status == "rejected" and not reason:
        raise HTTPException(status_code=422, detail="payment_rejection_reason_required")

    row.status = next_status
    row.reviewed_by = staff.id
    row.reviewed_at = _now()
    extra = dict(getattr(row, "extra_data", {}) or {})
    extra.update(
        {
            "reviewed_by": str(staff.id),
            "review_decision": next_status,
            "review_reason": reason,
            "review_policy": "finance_admin_only_enum_status",
        }
    )
    row.extra_data = extra
    order = None
    if row.order_id:
        order = (
            await session.execute(select(Order).where(Order.id == row.order_id).with_for_update())
        ).scalar_one_or_none()
        if order is not None and next_status == "approved":
            payments_model = MODEL_BY_TABLE["payments"]
            existing_payment = (
                await session.execute(
                    select(payments_model).where(
                        payments_model.order_id == order.id,
                        payments_model.extra_data["payment_receipt_id"].astext == str(row.id),
                    )
                )
            ).scalar_one_or_none()
            if existing_payment is None:
                session.add(
                    payments_model(
                        order_id=order.id,
                        user_id=row.user_id,
                        status="approved",
                        type="payment_receipt",
                        amount=money(row.amount),
                        extra_data={"payment_receipt_id": str(row.id), "reviewed_by": str(staff.id)},
                    )
                )
            await session.flush()
            await sync_order_payment_status(session, order)
        elif order is not None and next_status == "rejected":
            paid = await approved_payment_total(session, order.id)
            if paid <= 0:
                order.payment_status = "rejected"

    _add_audit_log(
        session,
        user_id=staff.id,
        action="payment_receipt_reviewed",
        description=f"Reviewed payment receipt {payment_id}",
        extra_data={"payment_receipt_id": payment_id, "status": next_status, "reason": reason},
    )
    await session.flush()
    await session.refresh(row)
    if order is not None:
        await session.refresh(order)
    payload = _safe_receipt_response(row, order)
    await session.commit()
    return payload


async def create_refund_request(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    body: dict[str, Any],
    staff: User,
    roles: set[str],
    idempotency_key: str,
    request_digest: str,
    endpoint: str,
) -> dict[str, Any]:
    require_finance_actor(roles)
    order = (
        await session.execute(select(Order).where(Order.id == order_id, Order.deleted_at.is_(None)).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    amount = money(body.get("amount"))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="refund_amount_required")
    paid = await approved_payment_total(session, order.id)
    already_refunded = await refunded_total(session, order.id)
    refundable = money(max(paid - already_refunded, Decimal("0.00")))
    if amount > refundable:
        raise HTTPException(status_code=409, detail="refund_exceeds_paid_amount")

    status_value = "requires_manual_action"
    refunds_model = MODEL_BY_TABLE["refunds"]
    row = refunds_model(
        order_id=order.id,
        user_id=order.user_id,
        status=status_value,
        amount=amount,
        reason=str(body.get("reason") or "refund"),
        extra_data={
            "requested_by": str(staff.id),
            "approved_by": None,
            "paid_before": str(paid),
            "refunded_before": str(already_refunded),
            "refundable_before": str(refundable),
            "provider_status": "blocked_credentials",
            "external_provider_refund": "blocked_credentials",
            "requires_manual_action": True,
            "manual_completion_required": True,
            "order_payment_status_before": order.payment_status,
            "idempotency_key": idempotency_key,
            "idempotency_actor_id": str(staff.id),
            "idempotency_endpoint": endpoint,
            "idempotency_request_hash": request_digest,
        },
    )
    session.add(row)
    await session.flush()
    await sync_order_payment_status(session, order)
    _add_audit_log(
        session,
        user_id=staff.id,
        action="refund.requested",
        description=f"Requested refund for order {order.order_number}",
        extra_data={"order_id": order.id, "refund_id": row.id, "amount": amount, "status": status_value},
    )
    await session.flush()
    await session.refresh(row)
    await session.refresh(order)
    payload = _safe_refund_response(row, order)
    await session.commit()
    return payload


async def update_refund_workflow_status(
    session: AsyncSession,
    *,
    refund_id: uuid.UUID,
    body: dict[str, Any],
    staff: User,
    roles: set[str],
) -> dict[str, Any]:
    require_finance_actor(roles)
    next_status = str(body.get("status") or "").strip().lower()
    if next_status == "processed":
        next_status = "manual_completed"
    if next_status not in REFUND_WORKFLOW_STATUSES:
        raise HTTPException(status_code=422, detail="invalid_refund_status")

    refunds_model = MODEL_BY_TABLE["refunds"]
    row = (
        await session.execute(select(refunds_model).where(refunds_model.id == refund_id, refunds_model.deleted_at.is_(None)).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="refund_not_found")
    current_status = str(row.status or "")
    if current_status in REFUND_COMPLETED_STATUSES:
        if current_status == next_status:
            order = await session.get(Order, row.order_id) if row.order_id else None
            if order is not None:
                # Keep derived order payment state correct on safe retries.
                # A completed refund may be replayed after another financial
                # mutation, so the idempotent path must still synchronize it.
                await sync_order_payment_status(session, order)
                await session.flush()
            payload = _safe_refund_response(row, order)
            payload["idempotency_replayed"] = True
            return payload
        raise HTTPException(status_code=409, detail="refund_already_completed")

    completion_evidence = _safe_text(
        body.get("completionEvidence")
        or body.get("completion_evidence")
        or body.get("providerReference")
        or body.get("provider_reference")
        or body.get("voucher_number"),
        max_len=500,
    )
    if next_status in REFUND_COMPLETED_STATUSES and not completion_evidence:
        raise HTTPException(status_code=422, detail="refund_completion_evidence_required")

    row.status = next_status
    extra = dict(getattr(row, "extra_data", {}) or {})
    extra.update(
        {
            "status_changed_by": str(staff.id),
            "status_changed_at": _now().isoformat(),
            "completion_evidence": completion_evidence,
            "provider_status": "manual_confirmed" if next_status in REFUND_COMPLETED_STATUSES else extra.get("provider_status"),
        }
    )
    order = None
    if row.order_id:
        order = (
            await session.execute(select(Order).where(Order.id == row.order_id).with_for_update())
        ).scalar_one_or_none()
        if order is not None and next_status in REFUND_COMPLETED_STATUSES:
            await apply_refund_reversals_once(session, order=order, refund_row=row, actor_id=staff.id, extra=extra)
            extra["requires_manual_action"] = False
            extra["manual_completion_required"] = False
            extra["completed_by"] = str(staff.id)
            extra["completed_at"] = _now().isoformat()
            row.extra_data = extra
            # SessionFactory intentionally disables autoflush for predictable
            # request transactions. Persist the completed refund state before
            # calculating the derived order payment status; otherwise the
            # aggregate query cannot see this refund and leaves the order as
            # `paid` instead of `partially_refunded`/`refunded`.
            await session.flush()
            await sync_order_payment_status(session, order)
        else:
            row.extra_data = extra
    else:
        row.extra_data = extra
    _add_audit_log(
        session,
        user_id=staff.id,
        action="refund.status_updated",
        description=f"Updated refund {refund_id}",
        extra_data={"refund_id": refund_id, "status": next_status},
    )
    await session.flush()
    await session.refresh(row)
    if order is not None:
        await session.refresh(order)
    payload = _safe_refund_response(row, order)
    await session.commit()
    return payload


async def apply_refund_reversals_once(
    session: AsyncSession,
    *,
    order: Order,
    refund_row: Any,
    actor_id: uuid.UUID,
    extra: dict[str, Any],
) -> None:
    if extra.get("reversals_applied_at"):
        return
    item_rows = list(
        (
            await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        ).scalars()
    )
    movement_model = MODEL_BY_TABLE["inventory_movements"]
    for item in item_rows:
        product = await session.get(Product, item.product_id) if item.product_id else None
        variant = await session.get(ProductVariant, item.variant_id) if item.variant_id else None
        if product is not None and product.track_inventory:
            if variant is not None:
                variant.stock_quantity = int(variant.stock_quantity or 0) + int(item.quantity or 0)
            else:
                product.stock_quantity = int(product.stock_quantity or 0) + int(item.quantity or 0)
            session.add(
                movement_model(
                    product_id=item.product_id,
                    variant_id=item.variant_id,
                    quantity=int(item.quantity or 0),
                    type="refund_reversal",
                    status="completed",
                    notes=f"Refund reversal for order {order.order_number}",
                    extra_data={"order_id": str(order.id), "refund_id": str(refund_row.id), "actor_id": str(actor_id)},
                )
            )

    loyalty_discount = money((order.extra_data or {}).get("financial_breakdown", {}).get("loyalty_discount", "0"))
    if loyalty_discount > 0:
        loyalty_model = MODEL_BY_TABLE["user_loyalty"]
        loyalty = (
            await session.execute(select(loyalty_model).where(loyalty_model.user_id == order.user_id).with_for_update().limit(1))
        ).scalar_one_or_none()
        if loyalty is not None:
            loyalty.balance = money(loyalty.balance or 0) + loyalty_discount
        tx_model = MODEL_BY_TABLE["points_transactions"]
        session.add(
            tx_model(
                user_id=order.user_id,
                order_id=order.id,
                type="refund_reversal",
                amount=loyalty_discount,
                description="Refund reversal of redeemed loyalty points",
                extra_data={"refund_id": str(refund_row.id), "actor_id": str(actor_id)},
            )
        )

    coupon_id = (order.extra_data or {}).get("coupon_id")
    if coupon_id:
        usage_model = MODEL_BY_TABLE["coupon_usage"]
        session.add(
            usage_model(
                user_id=order.user_id,
                order_id=order.id,
                amount=money((order.extra_data or {}).get("financial_breakdown", {}).get("coupon_discount", "0")),
                extra_data={"coupon_id": str(coupon_id), "refund_id": str(refund_row.id), "reversal": True},
            )
        )

    extra["reversals_applied_at"] = _now().isoformat()
    extra["reversals_applied_by"] = str(actor_id)


async def find_receipt_for_access(
    session: AsyncSession,
    *,
    receipt_ref: str,
    user: User,
    roles: set[str],
) -> Any:
    model = MODEL_BY_TABLE["payment_receipts"]
    ref = receipt_ref.strip()
    if ref.startswith("receipt:"):
        ref = ref.split(":", 1)[1]
    clauses = []
    try:
        clauses.append(model.id == uuid.UUID(ref))
    except ValueError:
        normalized = ref
        if normalized.startswith("/uploads/"):
            normalized = normalized.removeprefix("/uploads/")
        clauses.extend(
            [
                model.image_url == receipt_ref,
                model.extra_data["storage_key"].astext == normalized,
            ]
        )
    row = (
        await session.execute(select(model).where(or_(*clauses), model.deleted_at.is_(None)).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    if getattr(row, "user_id", None) != user.id and not roles.intersection(FINANCE_REVIEW_ROLES):
        raise HTTPException(status_code=404, detail="receipt_not_found")
    return row


async def find_file_asset_for_access(
    session: AsyncSession,
    *,
    receipt_ref: str,
    user: User,
    roles: set[str],
) -> FileAsset:
    """Resolve receipts uploaded through the shared private-file pipeline.

    Admin payment screens upload local/international receipts as FileAsset
    records (``file:<uuid>``).  They are intentionally not copied into the
    legacy payment_receipts table, so signed-url access must authorize that
    record directly.
    """
    ref = receipt_ref.strip()
    if ref.startswith("file:"):
        ref = ref.split(":", 1)[1]
    try:
        asset_id = uuid.UUID(ref)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="receipt_not_found") from exc
    asset = (
        await session.execute(
            select(FileAsset)
            .where(
                FileAsset.id == asset_id,
                FileAsset.deleted_at.is_(None),
                FileAsset.status == "available",
                FileAsset.scan_status.in_(("clean", "not_required")),
                FileAsset.policy_key == "payment_receipt",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    if (
        asset.owner_user_id != user.id
        and asset.created_by != user.id
        and not roles.intersection(FINANCE_REVIEW_ROLES)
    ):
        raise HTTPException(status_code=404, detail="receipt_not_found")
    return asset


def _file_asset_storage_path(asset: FileAsset, storage: FileStorage) -> Path:
    if str(asset.storage_provider or "").strip() == "cloudflare_r2":
        # Private receipt policies are kept on the backend filesystem.  Do
        # not silently turn a private R2 key into a local path.
        raise HTTPException(status_code=503, detail="private_receipt_storage_unavailable")
    target = storage._safe_join(str(asset.storage_key or ""))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="receipt_file_not_found")
    return target


def _receipt_storage_path(row: Any, storage: FileStorage) -> Path:
    extra = dict(getattr(row, "extra_data", {}) or {})
    storage_key = str(extra.get("storage_key") or "").strip()
    if not storage_key:
        image_url = str(getattr(row, "image_url", "") or "")
        if image_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="external_receipt_url_rejected")
        if image_url.startswith("/uploads/"):
            storage_key = image_url.removeprefix("/uploads/")
    if not storage_key or storage_key.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="receipt_storage_key_required")
    target = (storage.root / storage_key).resolve()
    if storage.root not in target.parents:
        raise HTTPException(status_code=400, detail="invalid_receipt_storage_path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="receipt_file_not_found")
    return target


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _signed_token(payload: dict[str, Any]) -> str:
    settings = get_settings()
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(settings.jwt_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64url(raw)}.{_b64url(signature)}"


def _verify_signed_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        raw_part, sig_part = token.split(".", 1)
        raw = _b64url_decode(raw_part)
        received = _b64url_decode(sig_part)
    except Exception:
        raise HTTPException(status_code=403, detail="invalid_receipt_token")
    expected = hmac.new(settings.jwt_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="invalid_receipt_token")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=403, detail="invalid_receipt_token")
    if int(payload.get("exp", 0)) < int(_now().timestamp()):
        raise HTTPException(status_code=403, detail="expired_receipt_token")
    return payload


async def issue_signed_receipt_url(
    session: AsyncSession,
    *,
    request: Request,
    receipt_ref: str,
    user: User,
    roles: set[str],
    storage: FileStorage,
    expires_in: int | None = None,
) -> dict[str, Any]:
    if expires_in is None:
        expires_in_effective = SIGNED_RECEIPT_MAX_AGE_SECONDS
    else:
        try:
            requested_seconds = int(expires_in)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_expires_in")
        if requested_seconds <= 0:
            raise HTTPException(status_code=422, detail="invalid_expires_in")
        expires_in_effective = min(
            max(requested_seconds, SIGNED_RECEIPT_MIN_AGE_SECONDS),
            SIGNED_RECEIPT_MAX_AGE_SECONDS,
        )
    if receipt_ref.strip().lower().startswith("file:"):
        asset = await find_file_asset_for_access(
            session,
            receipt_ref=receipt_ref,
            user=user,
            roles=roles,
        )
        target = _file_asset_storage_path(asset, storage)
        record_id = asset.id
        file_asset_id = str(asset.id)
    else:
        row = await find_receipt_for_access(session, receipt_ref=receipt_ref, user=user, roles=roles)
        target = _receipt_storage_path(row, storage)
        record_id = row.id
        file_asset_id = None
    exp = _now() + timedelta(seconds=expires_in_effective)
    payload = {
        "sub": str(user.id),
        "receipt_id": str(record_id),
        "storage_sha256": hashlib.sha256(str(target.relative_to(storage.root)).encode("utf-8")).hexdigest(),
        "exp": int(exp.timestamp()),
        "purpose": "payment_receipt_access",
    }
    if file_asset_id:
        payload["file_asset_id"] = file_asset_id
    token = _signed_token(payload)
    _add_audit_log(
        session,
        user_id=user.id,
        action="payment_receipt.signed_url_issued",
        description=f"Issued signed receipt access for {record_id}",
        extra_data={
            "payment_receipt_id": record_id if not file_asset_id else None,
            "file_asset_id": record_id if file_asset_id else None,
            "expires_at": exp.isoformat(),
        },
    )
    await session.commit()
    signed_url = f"{str(request.base_url).rstrip('/')}/receipts/access?token={token}"
    return {
        "signed_url": signed_url,
        "url": signed_url,
        "expires_at": exp.isoformat(),
        "expires_in_effective": expires_in_effective,
        "receipt_id": str(record_id),
        "file_asset_id": file_asset_id,
    }


async def signed_receipt_file_response(
    session: AsyncSession,
    *,
    token: str,
    storage: FileStorage,
) -> FileResponse:
    payload = _verify_signed_token(token)
    if payload.get("purpose") != "payment_receipt_access":
        raise HTTPException(status_code=403, detail="invalid_receipt_token")
    file_asset_id_raw = payload.get("file_asset_id")
    if file_asset_id_raw:
        try:
            file_asset_id = uuid.UUID(str(file_asset_id_raw))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=403, detail="invalid_receipt_token")
        asset = (
            await session.execute(
                select(FileAsset)
                .where(
                    FileAsset.id == file_asset_id,
                    FileAsset.deleted_at.is_(None),
                    FileAsset.status == "available",
                    FileAsset.scan_status.in_(("clean", "not_required")),
                    FileAsset.policy_key == "payment_receipt",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if asset is None:
            raise HTTPException(status_code=404, detail="receipt_not_found")
        target = _file_asset_storage_path(asset, storage)
        storage_hash = hashlib.sha256(str(target.relative_to(storage.root)).encode("utf-8")).hexdigest()
        if payload.get("storage_sha256") != storage_hash:
            raise HTTPException(status_code=403, detail="invalid_receipt_token")
        return FileResponse(
            target,
            media_type=str(asset.content_type or "application/octet-stream"),
            filename=str(asset.original_filename or target.name),
        )
    model = MODEL_BY_TABLE["payment_receipts"]
    row = (
        await session.execute(select(model).where(model.id == receipt_id, model.deleted_at.is_(None)).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="receipt_not_found")
    target = _receipt_storage_path(row, storage)
    storage_hash = hashlib.sha256(str(target.relative_to(storage.root)).encode("utf-8")).hexdigest()
    if payload.get("storage_sha256") != storage_hash:
        raise HTTPException(status_code=403, detail="invalid_receipt_token")
    extra = dict(getattr(row, "extra_data", {}) or {})
    return FileResponse(
        target,
        media_type=str(extra.get("mime_type") or "application/octet-stream"),
        filename=str(extra.get("original_filename") or target.name),
    )


async def receipt_database_audit(session: AsyncSession) -> dict[str, Any]:
    model = MODEL_BY_TABLE["payment_receipts"]
    rows = list((await session.execute(select(model).where(model.deleted_at.is_(None)))).scalars())
    base64_count = 0
    data_uri_count = 0
    external_url_count = 0
    invalid_status_count = 0
    base64_keys = {"base64", "database64", "data_base64", "filebase64", "file_base64", "imagebase64", "image_base64"}

    def has_encoded_payload(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).replace("-", "_").lower()
                if normalized_key in base64_keys:
                    return True
                if has_encoded_payload(item):
                    return True
            return False
        if isinstance(value, list):
            return any(has_encoded_payload(item) for item in value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("data:image", "data:application")):
                return True
            if len(stripped) > 2048 and all(ch.isalnum() or ch in "+/=_-" for ch in stripped[:2048]):
                return True
        return False

    def has_data_uri(value: Any) -> bool:
        if isinstance(value, dict):
            return any(has_data_uri(item) for item in value.values())
        if isinstance(value, list):
            return any(has_data_uri(item) for item in value)
        return isinstance(value, str) and value.strip().startswith(("data:image", "data:application"))

    def has_external_reference(value: Any) -> bool:
        if isinstance(value, dict):
            return any(has_external_reference(item) for item in value.values())
        if isinstance(value, list):
            return any(has_external_reference(item) for item in value)
        return isinstance(value, str) and value.strip().startswith(("http://", "https://"))

    for row in rows:
        extra = getattr(row, "extra_data", {}) or {}
        if has_encoded_payload(extra):
            base64_count += 1
        if has_data_uri(extra) or has_data_uri(getattr(row, "image_url", None)):
            data_uri_count += 1
        image_url = str(getattr(row, "image_url", "") or "")
        storage_key = str(extra.get("storage_key") or "")
        if image_url.startswith(("http://", "https://")) or storage_key.startswith(("http://", "https://")) or has_external_reference(extra):
            external_url_count += 1
        if str(row.status or "") not in RECEIPT_PENDING_STATUSES | RECEIPT_REVIEW_STATUSES:
            invalid_status_count += 1
    refunds_model = MODEL_BY_TABLE["refunds"]
    false_refunded_orders = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.payment_status == "refunded",
                    ~Order.id.in_(
                        select(refunds_model.order_id).where(
                            refunds_model.status.in_(tuple(REFUND_COMPLETED_STATUSES)),
                            refunds_model.deleted_at.is_(None),
                        )
                    ),
                )
            )
        ).scalar_one()
    )
    return {
        "receipt_records_checked": len(rows),
        "base64_receipt_records": base64_count,
        "data_uri_receipt_records": data_uri_count,
        "external_receipt_url_records": external_url_count,
        "invalid_receipt_status_values": invalid_status_count,
        "false_refunded_orders": false_refunded_orders,
    }
