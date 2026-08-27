from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import FileAsset, Order, OrderItem, Product, Profile, User, UserRole
from ..repositories.resources import serialize_record
from ..services.catalog_policy import build_public_product_rows, public_product_clauses
from ..services.financial_calculator import advisory_xact_lock, money
from ..services.notification_service import NotificationPayload, NotificationService
from ..services.realtime import RealtimeEventService, realtime_hub


AUTHORIZED_REPORT_ROLES = frozenset({"admin", "manager", "finance"})
AUTHORIZED_CUSTOMER_FULL_ROLES = frozenset({"admin", "manager"})
AUTHORIZED_CUSTOMER_LIMITED_ROLES = frozenset({"admin", "manager", "finance"})
AUTHORIZED_THEME_ROLES = frozenset({"admin", "manager"})
CAMPAIGN_ADMIN_ROLES = frozenset({"admin", "manager"})
SUPPORT_STAFF_ROLES = frozenset({"admin", "manager", "staff", "employee"})
COURIER_ACTIVE_STATUSES = frozenset({"active", "assigned", "accepted", "picked_up", "in_transit", "delivering", "out_for_delivery"})
REPORT_STATUSES = frozenset({"requested", "queued", "generating", "ready", "failed", "expired", "cancelled"})
REPORT_FORMATS = frozenset({"csv", "pdf"})
RECOGNIZED_PAYMENT_STATUSES = frozenset(
    {"paid", "confirmed", "approved", "captured", "settled", "completed", "partially_refunded"}
)
SUCCESSFUL_REFUND_STATUSES = frozenset(
    {"completed", "succeeded", "provider_succeeded", "manual_completed", "approved", "refunded"}
)
EXCLUDED_ORDER_STATUSES = frozenset({"cancelled", "canceled", "rejected", "failed", "void", "draft"})
PLACEHOLDER_TEXT = frozenset(
    {
        "support request",
        "new ticket",
        "no subject",
        "طلب دعم",
        "بدون عنوان",
        "test",
        "demo",
        "placeholder",
    }
)
_DANGEROUS_THEME_PATTERN = re.compile(r"(<script|javascript:|expression\s*\(|url\s*\(\s*javascript:)", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, datetime, date)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _parse_uuid(value: Any, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid_uuid:{field}") from exc


def _parse_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid_datetime:{field}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise HTTPException(status_code=422, detail=f"invalid_datetime:{field}")


def _parse_day(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or _now().date()).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_operational_day_date") from exc


def _date_range(start: Any = None, end: Any = None) -> tuple[datetime | None, datetime | None]:
    start_dt = _parse_datetime(start, "start") if start not in (None, "") else None
    end_dt = _parse_datetime(end, "end") if end not in (None, "") else None
    if isinstance(end, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end.strip()) and end_dt is not None:
        end_dt = end_dt + timedelta(days=1) - timedelta(microseconds=1)
    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=422, detail="invalid_date_range")
    return start_dt, end_dt


def _require_roles(roles: set[str], allowed: frozenset[str], detail: str) -> None:
    if not roles.intersection(allowed):
        raise HTTPException(status_code=403, detail=detail)


def _safe_csv_cell(value: Any) -> str:
    text = "" if value is None else str(_jsonable(value))
    if text[:1] in {"=", "+", "-", "@"}:
        return f"'{text}"
    return text


def _hash_device(device_id: str, user_id: uuid.UUID, stream: str, platform: str) -> str:
    raw = f"{user_id}:{stream}:{platform}:{device_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _theme_payload_safe(value: Any) -> bool:
    if isinstance(value, str):
        return not _DANGEROUS_THEME_PATTERN.search(value)
    if isinstance(value, dict):
        return all(_theme_payload_safe(item) for item in value.values())
    if isinstance(value, list):
        return all(_theme_payload_safe(item) for item in value)
    return True


def _report_file_path(relative_path: str) -> Path:
    settings = get_settings()
    root = settings.resolved_upload_dir.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="report_file_not_found") from exc
    return target


@dataclass(frozen=True)
class RecognizedOrderRevenue:
    order_id: uuid.UUID
    order_number: str
    order_total: Decimal
    partner_share_gross: Decimal
    payment_total: Decimal
    refund_total: Decimal
    net_revenue: Decimal
    currency_code: str
    status: str
    payment_status: str


class RevenueRecognitionService:
    @staticmethod
    async def eligible_orders(
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
    ) -> list[Order]:
        start_dt, end_dt = _date_range(start, end)
        statement = select(Order).where(
            Order.deleted_at.is_(None),
            ~func.lower(Order.status).in_(tuple(EXCLUDED_ORDER_STATUSES)),
        )
        if start_dt is not None:
            statement = statement.where(Order.created_at >= start_dt)
        if end_dt is not None:
            statement = statement.where(Order.created_at <= end_dt)
        return list((await session.execute(statement.order_by(Order.created_at.desc()))).scalars())

    @staticmethod
    async def order_rows(
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
        partner_id: uuid.UUID | None = None,
    ) -> list[RecognizedOrderRevenue]:
        orders = await RevenueRecognitionService.eligible_orders(session, start=start, end=end)
        if not orders:
            return []
        if partner_id is not None:
            partner_order_ids = set(
                (
                    await session.execute(
                        select(OrderItem.order_id).where(
                            OrderItem.partner_id == partner_id,
                            OrderItem.order_id.in_([order.id for order in orders]),
                        )
                    )
                ).scalars()
            )
            orders = [order for order in orders if order.id in partner_order_ids]
            if not orders:
                return []
        order_ids = [row.id for row in orders]
        payment_model = MODEL_BY_TABLE["order_payments"]
        refund_model = MODEL_BY_TABLE["refunds"]
        payments_result = await session.execute(
            select(payment_model)
            .where(
                payment_model.order_id.in_(order_ids),
                payment_model.deleted_at.is_(None),
                func.lower(payment_model.status).in_(tuple(RECOGNIZED_PAYMENT_STATUSES)),
            )
        )
        refunds_result = await session.execute(
            select(refund_model)
            .where(
                refund_model.order_id.in_(order_ids),
                refund_model.deleted_at.is_(None),
                func.lower(refund_model.status).in_(tuple(SUCCESSFUL_REFUND_STATUSES)),
            )
        )
        item_result = await session.execute(select(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        payments_by_order: dict[uuid.UUID, Decimal] = {}
        refunds_by_order: dict[uuid.UUID, Decimal] = {}
        partner_share_by_order: dict[uuid.UUID, Decimal] = {}
        for payment in payments_result.scalars():
            payments_by_order[payment.order_id] = money(payments_by_order.get(payment.order_id, 0) + money(payment.amount or 0))
        for refund in refunds_result.scalars():
            refunds_by_order[refund.order_id] = money(refunds_by_order.get(refund.order_id, 0) + money(refund.amount or 0))
        for item in item_result.scalars():
            if partner_id is not None and item.partner_id != partner_id:
                continue
            partner_share_by_order[item.order_id] = money(partner_share_by_order.get(item.order_id, 0) + money(item.total_price or 0))

        rows: list[RecognizedOrderRevenue] = []
        for order in orders:
            payment_total = payments_by_order.get(order.id, Decimal("0.00"))
            if payment_total <= 0 and str(order.payment_status or "").lower() in RECOGNIZED_PAYMENT_STATUSES:
                payment_total = money(getattr(order, "total", 0))
            if payment_total <= 0:
                continue
            order_total = money(order.total or 0)
            gross_share = partner_share_by_order.get(order.id, order_total if partner_id is None else Decimal("0.00"))
            if partner_id is not None and gross_share <= 0:
                continue
            refund_total = refunds_by_order.get(order.id, Decimal("0.00"))
            if partner_id is not None and order_total > 0 and refund_total > 0:
                refund_total = money(refund_total * (gross_share / order_total))
            net_revenue = money(max(Decimal("0.00"), min(gross_share, payment_total) - refund_total))
            rows.append(
                RecognizedOrderRevenue(
                    order_id=order.id,
                    order_number=order.order_number,
                    order_total=order_total,
                    partner_share_gross=gross_share,
                    payment_total=payment_total,
                    refund_total=refund_total,
                    net_revenue=net_revenue,
                    currency_code=order.currency_code or "YER",
                    status=order.status,
                    payment_status=order.payment_status,
                )
            )
        return rows

    @classmethod
    async def summary(cls, session: AsyncSession, *, start: Any = None, end: Any = None, partner_id: uuid.UUID | None = None) -> dict[str, Any]:
        rows = await cls.order_rows(session, start=start, end=end, partner_id=partner_id)
        gross = money(sum((row.partner_share_gross for row in rows), Decimal("0.00")))
        refunds = money(sum((row.refund_total for row in rows), Decimal("0.00")))
        net = money(sum((row.net_revenue for row in rows), Decimal("0.00")))
        paid = money(sum((row.payment_total for row in rows), Decimal("0.00")))
        return {
            "date_basis": "orders.created_at plus successful payment/refund status",
            "order_count": len(rows),
            "eligible_order_count": len(rows),
            "gross_revenue": format(gross, "f"),
            "paid_amount": format(paid, "f"),
            "refund_amount": format(refunds, "f"),
            "net_revenue": format(net, "f"),
            "currency_code": rows[0].currency_code if rows else "YER",
            "partner_scope": str(partner_id) if partner_id else None,
        }

    @classmethod
    async def report_source(cls, session: AsyncSession, *, start: Any = None, end: Any = None) -> dict[str, Any]:
        all_orders = await cls.eligible_orders(session, start=start, end=end)
        revenue_rows = await cls.order_rows(session, start=start, end=end)
        order_ids = [row.id for row in all_orders]
        orders = [serialize_record(row) for row in all_orders]
        item_rows = []
        if order_ids:
            result = await session.execute(select(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            item_rows = [serialize_record(row) for row in result.scalars()]
        profile_result = await session.execute(
            select(Profile)
            .join(UserRole, UserRole.user_id == Profile.user_id)
            .where(Profile.deleted_at.is_(None), UserRole.role == "customer")
            .order_by(Profile.created_at.desc())
            .limit(500)
        )
        profiles = [serialize_record(row) for row in profile_result.scalars()]
        summary = await cls.summary(session, start=start, end=end)
        return {
            "orders": orders,
            "items": item_rows,
            "profiles": profiles,
            "marketerCommissions": [],
            "partnerCommissions": [],
            "revenue": summary,
            "recognizedOrders": [_jsonable(row.__dict__) for row in revenue_rows],
        }


class ReportGenerationService:
    DEFINITIONS = {
        "orders": ("orders", ("order_id", "order_number", "status", "payment_status", "gross", "paid", "refunds", "net")),
        "sales": ("orders", ("order_id", "order_number", "status", "payment_status", "gross", "paid", "refunds", "net")),
        "revenue": ("revenue", ("metric", "value")),
        "summary": ("revenue", ("metric", "value")),
        "customers": ("customers", ("customer_id", "name", "email", "classification", "created_at", "orders", "total_spent")),
        "merchant_revenue": ("merchant_revenue", ("order_id", "order_number", "merchant_gross", "refunds", "net")),
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def require_access(roles: set[str]) -> None:
        _require_roles(roles, AUTHORIZED_REPORT_ROLES, "report_permission_denied")

    async def generate_export(
        self,
        request: Request,
        *,
        actor: User,
        roles: set[str],
        body: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.require_access(roles)
        report_type = str(body.get("type") or body.get("reportType") or "summary").strip().lower()
        fmt = str(body.get("format") or body.get("fileType") or "csv").strip().lower()
        if report_type not in self.DEFINITIONS:
            raise HTTPException(status_code=422, detail="unsupported_report_type")
        if fmt not in REPORT_FORMATS:
            raise HTTPException(status_code=422, detail="unsupported_report_format")
        key = str(idempotency_key or body.get("idempotencyKey") or body.get("idempotency_key") or "").strip()
        if key:
            await advisory_xact_lock(self.session, f"report-export:{actor.id}:{key}")
            existing = await self._find_idempotent(actor.id, key)
            if existing is not None:
                return self._response(existing, request)
        model = MODEL_BY_TABLE["report_exports"]
        row = model(
            user_id=actor.id,
            type=report_type,
            status="requested",
            path="",
            description="Official PostgreSQL report export",
            extra_data={
                "format": fmt,
                "idempotency_key": key or None,
                "requested_at": _now().isoformat(),
                "filters": {k: _jsonable(v) for k, v in body.items() if k not in {"idempotencyKey", "idempotency_key"}},
            },
        )
        self.session.add(row)
        await self.session.flush()
        try:
            row.status = "generating"
            rows, columns, metadata = await self._query_rows(report_type, body)
            data, content_type, extension = self._render(report_type, fmt, rows, columns, metadata)
            asset = await self._save_file(row.id, actor.id, fmt, extension, content_type, data)
            target_path = _report_file_path(asset.storage_key)
            if not target_path.is_file() or target_path.stat().st_size <= 0:
                raise HTTPException(status_code=500, detail="report_file_generation_failed")
            row.status = "ready"
            row.path = asset.storage_key
            row.extra_data = {
                **(row.extra_data or {}),
                "file_id": str(asset.id),
                "storage_key": asset.storage_key,
                "sha256": asset.checksum_sha256,
                "size_bytes": asset.size_bytes,
                "content_type": asset.content_type,
                "ready_at": _now().isoformat(),
                "row_count": len(rows),
                "metadata": metadata,
            }
            self._audit(actor.id, "report_export.ready", f"Generated {report_type} {fmt} report")
        except HTTPException as exc:
            row.status = "failed"
            row.path = ""
            row.extra_data = {**(row.extra_data or {}), "failed_at": _now().isoformat(), "error": str(exc.detail)}
            self._audit(actor.id, "report_export.failed", f"Failed {report_type} report")
            await self.session.commit()
            raise
        except Exception as exc:
            row.status = "failed"
            row.path = ""
            row.extra_data = {**(row.extra_data or {}), "failed_at": _now().isoformat(), "error": exc.__class__.__name__}
            self._audit(actor.id, "report_export.failed", f"Failed {report_type} report")
            await self.session.commit()
            raise HTTPException(status_code=500, detail="report_generation_failed") from exc
        await self.session.commit()
        await self.session.refresh(row)
        return self._response(row, request)

    async def list_exports(self, request: Request, *, actor: User, roles: set[str], limit: int = 500) -> list[dict[str, Any]]:
        self.require_access(roles)
        model = MODEL_BY_TABLE["report_exports"]
        statement = select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(min(limit, 500))
        if not roles.intersection({"admin", "manager"}):
            statement = statement.where(model.user_id == actor.id)
        return [self._response(row, request) for row in (await self.session.execute(statement)).scalars()]

    async def download(self, export_id: uuid.UUID, *, actor: User, roles: set[str]) -> FileResponse:
        self.require_access(roles)
        row = await self.session.get(MODEL_BY_TABLE["report_exports"], export_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="report_export_not_found")
        if not roles.intersection({"admin", "manager"}) and row.user_id != actor.id:
            raise HTTPException(status_code=403, detail="report_export_access_denied")
        if row.status != "ready" or not row.path:
            raise HTTPException(status_code=409, detail="report_export_not_ready")
        path = _report_file_path(row.path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise HTTPException(status_code=404, detail="report_file_not_found")
        content_type = (row.extra_data or {}).get("content_type") or "application/octet-stream"
        return FileResponse(
            path,
            media_type=content_type,
            filename=path.name,
            headers={"Cache-Control": "no-store", "X-Report-Export-ID": str(row.id)},
        )

    async def _find_idempotent(self, actor_id: uuid.UUID, key: str) -> Any | None:
        model = MODEL_BY_TABLE["report_exports"]
        result = await self.session.execute(
            select(model)
            .where(
                model.user_id == actor_id,
                model.deleted_at.is_(None),
                model.extra_data["idempotency_key"].astext == key,
                model.status != "failed",
            )
            .order_by(model.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _query_rows(self, report_type: str, body: dict[str, Any]) -> tuple[list[dict[str, Any]], tuple[str, ...], dict[str, Any]]:
        start = body.get("dateFrom") or body.get("date_from") or body.get("start")
        end = body.get("dateTo") or body.get("date_to") or body.get("end")
        partner_id = _parse_uuid(body.get("partnerId") or body.get("partner_id"), "partner_id") if body.get("partnerId") or body.get("partner_id") else None
        if report_type in {"orders", "sales"}:
            orders = await RevenueRecognitionService.eligible_orders(self.session, start=start, end=end)
            recognized = await RevenueRecognitionService.order_rows(self.session, start=start, end=end)
            recognized_by_id = {row.order_id: row for row in recognized}
            return (
                [
                    {
                        "order_id": str(order.id),
                        "order_number": order.order_number,
                        "status": order.status,
                        "payment_status": order.payment_status,
                        "gross": format(recognized_by_id.get(order.id).partner_share_gross if order.id in recognized_by_id else money(order.total or 0), "f"),
                        "paid": format(recognized_by_id.get(order.id).payment_total if order.id in recognized_by_id else money(0), "f"),
                        "refunds": format(recognized_by_id.get(order.id).refund_total if order.id in recognized_by_id else money(0), "f"),
                        "net": format(recognized_by_id.get(order.id).net_revenue if order.id in recognized_by_id else money(0), "f"),
                    }
                    for order in orders
                ],
                self.DEFINITIONS[report_type][1],
                await RevenueRecognitionService.summary(self.session, start=start, end=end),
            )
        if report_type == "customers":
            orders = await RevenueRecognitionService.eligible_orders(self.session, start=start, end=end)
            recognized = await RevenueRecognitionService.order_rows(self.session, start=start, end=end)
            recognized_by_id = {row.order_id: row for row in recognized}
            order_counts: dict[uuid.UUID, int] = {}
            spending: dict[uuid.UUID, Decimal] = {}
            for order in orders:
                if not order.user_id:
                    continue
                order_counts[order.user_id] = order_counts.get(order.user_id, 0) + 1
                recognized_order = recognized_by_id.get(order.id)
                if recognized_order is not None:
                    spending[order.user_id] = money(spending.get(order.user_id, Decimal("0.00")) + recognized_order.net_revenue)
            profile_result = await self.session.execute(
                select(Profile)
                .join(UserRole, UserRole.user_id == Profile.user_id)
                .where(Profile.deleted_at.is_(None), UserRole.role == "customer")
                .distinct()
                .order_by(Profile.created_at.desc())
                .limit(500)
            )
            profiles = list(profile_result.scalars())
            return (
                [
                    {
                        "customer_id": str(profile.user_id),
                        "name": profile.full_name or "غير معروف",
                        "email": profile.email or "-",
                        "classification": profile.classification or "normal",
                        "created_at": str(profile.created_at or ""),
                        "orders": order_counts.get(profile.user_id, 0),
                        "total_spent": format(spending.get(profile.user_id, Decimal("0.00")), "f"),
                    }
                    for profile in profiles
                ],
                self.DEFINITIONS[report_type][1],
                await RevenueRecognitionService.summary(self.session, start=start, end=end),
            )
        if report_type == "merchant_revenue":
            if partner_id is None:
                raise HTTPException(status_code=422, detail="partner_id_required")
            rows = await RevenueRecognitionService.order_rows(self.session, start=start, end=end, partner_id=partner_id)
            return (
                [
                    {
                        "order_id": str(row.order_id),
                        "order_number": row.order_number,
                        "merchant_gross": format(row.partner_share_gross, "f"),
                        "refunds": format(row.refund_total, "f"),
                        "net": format(row.net_revenue, "f"),
                    }
                    for row in rows
                ],
                self.DEFINITIONS[report_type][1],
                await RevenueRecognitionService.summary(self.session, start=start, end=end, partner_id=partner_id),
            )
        summary = await RevenueRecognitionService.summary(self.session, start=start, end=end)
        return ([{"metric": key, "value": value} for key, value in summary.items()], self.DEFINITIONS["summary"][1], summary)

    def _render(
        self,
        report_type: str,
        fmt: str,
        rows: list[dict[str, Any]],
        columns: tuple[str, ...],
        metadata: dict[str, Any],
    ) -> tuple[bytes, str, str]:
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: _safe_csv_cell(row.get(column)) for column in columns})
            data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
            return data, "text/csv; charset=utf-8", ".csv"
        return self._render_pdf(report_type, rows, columns, metadata), "application/pdf", ".pdf"

    @staticmethod
    def _render_pdf(report_type: str, rows: list[dict[str, Any]], columns: tuple[str, ...], metadata: dict[str, Any]) -> bytes:
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.pdfgen import canvas
        except Exception as exc:
            raise HTTPException(status_code=503, detail="pdf_renderer_unavailable") from exc
        font_name = "Helvetica"
        font_path = Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Tajawal-Regular.ttf"
        if font_path.is_file():
            try:
                pdfmetrics.registerFont(TTFont("Tajawal", str(font_path)))
                font_name = "Tajawal"
            except Exception:
                font_name = "Helvetica"
        buffer = io.BytesIO()
        doc = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        y = height - 50
        doc.setFont(font_name, 14)
        doc.drawString(40, y, f"Luxury Report: {report_type}")
        y -= 24
        doc.setFont(font_name, 9)
        for key, value in metadata.items():
            doc.drawString(40, y, f"{key}: {_safe_csv_cell(value)}")
            y -= 14
            if y < 80:
                doc.showPage()
                doc.setFont(font_name, 9)
                y = height - 50
        y -= 10
        header = " | ".join(columns)
        doc.drawString(40, y, header[:140])
        y -= 16
        for row in rows:
            values = " | ".join(_safe_csv_cell(row.get(column)) for column in columns)
            for start in range(0, len(values), 140):
                doc.drawString(40, y, values[start : start + 140])
                y -= 13
                if y < 50:
                    doc.showPage()
                    doc.setFont(font_name, 9)
                    y = height - 50
        doc.save()
        return buffer.getvalue()

    async def _save_file(
        self,
        report_id: uuid.UUID,
        actor_id: uuid.UUID,
        fmt: str,
        extension: str,
        content_type: str,
        data: bytes,
    ) -> FileAsset:
        if not data:
            raise HTTPException(status_code=500, detail="empty_report_file")
        settings = get_settings()
        relative = f"_private/reports/{report_id}{extension}"
        target = _report_file_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
        tmp.replace(target)
        size = target.stat().st_size
        checksum = hashlib.sha256(data).hexdigest()
        asset = FileAsset(
            owner_user_id=None,
            created_by=actor_id,
            policy_key="report_export",
            visibility="private",
            storage_provider="local_uploads",
            storage_bucket="report_export",
            storage_key=relative,
            original_filename=f"{report_id}{extension}",
            content_type=content_type,
            size_bytes=size,
            checksum_sha256=checksum,
            status="available",
            scan_status="not_required" if fmt == "csv" else "clean",
            scan_provider="server-generated",
            entity_type="report_exports",
            entity_id=report_id,
            extra_data={"generated_by": str(actor_id), "storage_environment": settings.storage_environment},
        )
        self.session.add(asset)
        await self.session.flush()
        return asset

    def _response(self, row: Any, request: Request) -> dict[str, Any]:
        payload = serialize_record(row)
        file_id = (row.extra_data or {}).get("file_id")
        payload["file_id"] = file_id
        payload["download_url"] = f"{str(request.base_url).rstrip('/')}/reports/exports/{row.id}/download" if row.status == "ready" else None
        payload["ready_has_valid_file"] = bool(row.status == "ready" and row.path and _report_file_path(row.path).is_file())
        return payload

    def _audit(self, user_id: uuid.UUID, action: str, description: str) -> None:
        model = MODEL_BY_TABLE["audit_logs"]
        self.session.add(model(user_id=user_id, type=action, description=description))


class AdminCustomerAccessService:
    @staticmethod
    async def list_customers(session: AsyncSession, *, roles: set[str], limit: int = 500, full: bool = False) -> list[dict[str, Any]]:
        if full:
            _require_roles(roles, AUTHORIZED_CUSTOMER_FULL_ROLES, "customer_full_access_denied")
        else:
            _require_roles(roles, AUTHORIZED_CUSTOMER_LIMITED_ROLES, "customer_access_denied")
        limit = min(max(int(limit), 1), 500)
        result = await session.execute(
            select(User, Profile)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.deleted_at.is_(None))
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        user_ids = []
        rows = []
        for user, profile in result.all():
            user_ids.append(user.id)
            row = {
                "id": str(user.id),
                "user_id": str(user.id),
                "email": user.email,
                "is_active": user.is_active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "profile": {
                    "id": str(profile.id) if profile else None,
                    "full_name": profile.full_name if profile else None,
                    "phone": profile.phone if profile else None,
                    "city": profile.city if profile else None,
                },
            }
            if full:
                profile_extra = dict(profile.extra_data or {}) if profile else {}
                row["roles"] = []
                row["profile"].update(
                    {
                        "avatar_url": profile.avatar_url if profile else None,
                        "classification": profile.classification if profile else None,
                        "admin_notes": profile_extra.get("admin_notes"),
                    }
                )
            rows.append(row)
        if full and user_ids:
            role_result = await session.execute(select(UserRole.user_id, UserRole.role).where(UserRole.user_id.in_(user_ids)))
            role_map: dict[uuid.UUID, list[str]] = {}
            for user_id, role in role_result.all():
                role_map.setdefault(user_id, []).append(role)
            for row in rows:
                row["roles"] = sorted(role_map.get(uuid.UUID(row["user_id"]), []))
        return rows


class CampaignService:
    @staticmethod
    def require_access(roles: set[str]) -> None:
        _require_roles(roles, CAMPAIGN_ADMIN_ROLES, "campaign_permission_denied")

    @staticmethod
    def _normalize_body(body: dict[str, Any]) -> dict[str, Any]:
        title = str(body.get("title") or body.get("name") or "").strip()
        message = str(
            body.get("message")
            or body.get("body")
            or body.get("content")
            or body.get("subtitle")
            or body.get("title")
            or ""
        ).strip()
        if len(title) < 3:
            raise HTTPException(status_code=422, detail="campaign_title_required")
        if len(message) < 3:
            raise HTTPException(status_code=422, detail="campaign_message_required")
        scheduled_at = _parse_datetime(body.get("scheduledAt") or body.get("scheduled_at"), "scheduled_at")
        channels = body.get("channels") or [body.get("channel") or "in_app"]
        if not isinstance(channels, list):
            channels = [channels]
        allowed_channels = {"in_app", "push", "email", "whatsapp"}
        clean_channels = sorted({str(item).strip().lower() for item in channels if str(item).strip().lower() in allowed_channels})
        if not clean_channels:
            raise HTTPException(status_code=422, detail="campaign_channel_required")
        audience = str(body.get("audience") or body.get("targetAudience") or "all_active_users").strip()
        return {
            "title": title,
            "message": message,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "channels": clean_channels,
            "audience": audience,
            "consent_required": bool(body.get("consentRequired", body.get("consent_required", True))),
            "dedupe_key": str(body.get("dedupeKey") or body.get("dedupe_key") or hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]),
        }

    async def create(self, session: AsyncSession, *, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        normalized = self._normalize_body(body)
        status = "draft" if body.get("saveAsDraft") is True else (
            "scheduled" if normalized["scheduled_at"] else "queued"
        )
        model = MODEL_BY_TABLE["marketing_campaigns"]
        row = model(
            title=normalized["title"],
            message=normalized["message"],
            status=status,
            created_by=actor.id,
            extra_data={**body, **normalized, "created_at": _now().isoformat(), "metrics": {"queued": 0, "sent": 0, "failed": 0}},
        )
        session.add(row)
        self._audit(session, actor.id, "campaign.create", "Created marketing campaign")
        await session.commit()
        await session.refresh(row)
        return await self.response(session, row)

    async def update(self, session: AsyncSession, *, campaign_id: uuid.UUID, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        row = await session.get(MODEL_BY_TABLE["marketing_campaigns"], campaign_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="campaign_not_found")
        existing = {
            "title": row.title,
            "message": row.message,
            **(row.extra_data or {}),
        }
        normalized = self._normalize_body({**existing, **body})
        if row.status in {"completed", "cancelled"}:
            raise HTTPException(status_code=409, detail="campaign_not_editable")
        row.title = normalized["title"]
        row.message = normalized["message"]
        row.status = str(body.get("status") or ("scheduled" if normalized["scheduled_at"] else "queued"))
        row.extra_data = {**(row.extra_data or {}), **body, **normalized, "updated_at": _now().isoformat()}
        self._audit(session, actor.id, "campaign.update", "Updated marketing campaign")
        await session.commit()
        return await self.response(session, row)

    async def schedule(self, session: AsyncSession, *, campaign_id: uuid.UUID, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        row = await session.get(MODEL_BY_TABLE["marketing_campaigns"], campaign_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="campaign_not_found")
        scheduled_at = _parse_datetime(body.get("scheduledAt") or body.get("scheduled_at") or _now().isoformat(), "scheduled_at")
        row.status = "scheduled"
        row.extra_data = {**(row.extra_data or {}), "scheduled_at": scheduled_at.isoformat(), "scheduled_by": str(actor.id)}
        self._audit(session, actor.id, "campaign.schedule", "Scheduled marketing campaign")
        await session.commit()
        return await self.response(session, row)

    async def list(self, session: AsyncSession, *, roles: set[str], limit: int = 500) -> list[dict[str, Any]]:
        self.require_access(roles)
        model = MODEL_BY_TABLE["marketing_campaigns"]
        result = await session.execute(
            select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(min(limit, 500))
        )
        return [await self.response(session, row) for row in result.scalars()]

    async def preview(self, session: AsyncSession, *, campaign_id: uuid.UUID, roles: set[str]) -> dict[str, Any]:
        self.require_access(roles)
        row = await session.get(MODEL_BY_TABLE["marketing_campaigns"], campaign_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="campaign_not_found")
        recipient_ids = await self._audience(session, row)
        return {
            "data": {
                "campaign_id": str(row.id),
                "status": row.status,
                "audience_count": len(recipient_ids),
                "channels": (row.extra_data or {}).get("channels") or ["in_app"],
                "payload": {"title": row.title, "message": row.message},
            }
        }

    async def record_event(self, session: AsyncSession, *, campaign_id: uuid.UUID, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        field = str(body.get("field") or body.get("event") or "event").strip().lower()
        if field not in {"sent", "delivered", "opened", "clicked", "converted", "failed"}:
            raise HTTPException(status_code=422, detail="unsupported_campaign_event")
        event_model = MODEL_BY_TABLE["analytics_events"]
        session.add(event_model(user_id=actor.id, type=f"campaign_{field}", description=str(campaign_id), extra_data={**body, "campaign_id": str(campaign_id)}))
        await session.commit()
        return {"ok": True, "metrics": await self.metrics(session, campaign_id)}

    async def process_due(self, session: AsyncSession, *, limit: int = 50) -> dict[str, Any]:
        model = MODEL_BY_TABLE["marketing_campaigns"]
        now_text = _now().isoformat()
        result = await session.execute(
            select(model)
            .where(
                model.deleted_at.is_(None),
                model.status.in_(("queued", "scheduled")),
                or_(model.extra_data["scheduled_at"].astext.is_(None), model.extra_data["scheduled_at"].astext <= now_text),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        campaigns = list(result.scalars())
        processed = 0
        delivered = 0
        blocked_credentials = 0
        for row in campaigns:
            row.status = "processing"
            recipient_ids = await self._audience(session, row)
            channels = (row.extra_data or {}).get("channels") or ["in_app"]
            sent = await self._deliver_batch(session, row, recipient_ids, channels)
            processed += 1
            delivered += sent["sent"]
            blocked_credentials += sent["blocked_credentials"]
            final_status = "completed" if sent["blocked_credentials"] == 0 else "blocked_credentials"
            row.status = final_status
            row.extra_data = {
                **(row.extra_data or {}),
                "processed_at": _now().isoformat(),
                "metrics": await self.metrics(session, row.id),
                "blocked_credentials": sent["blocked_credentials"],
            }
        return {"processed": processed, "sent": delivered, "blocked_credentials": blocked_credentials}

    async def _audience(self, session: AsyncSession, row: Any) -> list[uuid.UUID]:
        audience = str((row.extra_data or {}).get("audience") or "all_active_users")
        statement = select(User.id).where(User.deleted_at.is_(None), User.is_active.is_(True))
        if audience in {"customers", "customer"}:
            statement = statement.where(User.id.in_(select(UserRole.user_id).where(UserRole.role == "customer")))
        result = await session.execute(statement.limit(5000))
        return list(result.scalars())

    async def _deliver_batch(self, session: AsyncSession, row: Any, recipients: list[uuid.UUID], channels: list[str]) -> dict[str, int]:
        event_model = MODEL_BY_TABLE["analytics_events"]
        settings = get_settings()
        sent = 0
        blocked_credentials = 0
        notification = NotificationService(session)
        for recipient_id in recipients:
            for channel in channels:
                dedupe = f"campaign:{row.id}:{recipient_id}:{channel}"
                exists = (
                    await session.execute(
                        select(event_model.id)
                        .where(
                            event_model.type == "campaign_delivery",
                            event_model.description == str(row.id),
                            event_model.extra_data["dedupe_key"].astext == dedupe,
                            event_model.deleted_at.is_(None),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                status = "sent"
                if settings.app_env != "test" and channel in {"email", "whatsapp", "push"}:
                    status = "blocked_credentials"
                    blocked_credentials += 1
                else:
                    sent += 1
                    if channel in {"in_app", "push"}:
                        await notification.create_notification(
                            NotificationPayload(
                                user_id=recipient_id,
                                title=row.title,
                                body=row.message,
                                notification_type="marketing_campaign",
                                category="marketing",
                                priority="normal",
                                payload={"campaign_id": str(row.id), "channel": channel},
                                created_by=row.created_by,
                                source="campaign_worker",
                                deduplication_key=dedupe,
                            )
                        )
                session.add(
                    event_model(
                        user_id=recipient_id,
                        type="campaign_delivery",
                        description=str(row.id),
                        extra_data={"campaign_id": str(row.id), "channel": channel, "status": status, "dedupe_key": dedupe},
                    )
                )
        return {"sent": sent, "blocked_credentials": blocked_credentials}

    async def metrics(self, session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
        event_model = MODEL_BY_TABLE["analytics_events"]
        status_expr = event_model.extra_data["status"].astext.label("delivery_status")
        result = await session.execute(
            select(event_model.type, status_expr, func.count())
            .where(event_model.description == str(campaign_id), event_model.deleted_at.is_(None))
            .group_by(event_model.type, status_expr)
        )
        metrics = {"sent": 0, "failed": 0, "blocked_credentials": 0, "opened": 0, "clicked": 0, "converted": 0}
        for event_type, status, count in result.all():
            key = str(status or event_type).replace("campaign_", "")
            if key in metrics:
                metrics[key] += int(count)
        return metrics

    async def response(self, session: AsyncSession, row: Any) -> dict[str, Any]:
        payload = serialize_record(row)
        payload["metrics"] = await self.metrics(session, row.id)
        return payload

    @staticmethod
    def _audit(session: AsyncSession, user_id: uuid.UUID, action: str, description: str) -> None:
        session.add(MODEL_BY_TABLE["audit_logs"](user_id=user_id, type=action, description=description))


class CourierLocationService:
    @staticmethod
    def _coordinate(value: Any, *, field: str, low: Decimal, high: Decimal) -> Decimal:
        try:
            numeric = Decimal(str(value))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid_coordinate:{field}") from exc
        if not numeric.is_finite() or math.isnan(float(numeric)) or numeric < low or numeric > high:
            raise HTTPException(status_code=422, detail=f"invalid_coordinate:{field}")
        return numeric.quantize(Decimal("0.0000001"))

    async def record(self, session: AsyncSession, *, user: User, body: dict[str, Any]) -> dict[str, Any]:
        assignment_id = _parse_uuid(body.get("assignmentId") or body.get("assignment_id"), "assignment_id")
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        assignment = (
            await session.execute(
                select(assignment_model)
                .where(
                    assignment_model.id == assignment_id,
                    assignment_model.deleted_at.is_(None),
                    or_(assignment_model.user_id == user.id, assignment_model.courier_id == user.id),
                    assignment_model.status.in_(tuple(COURIER_ACTIVE_STATUSES)),
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if assignment is None:
            raise HTTPException(status_code=404, detail="assignment_not_found")
        lat = self._coordinate(body.get("latitude"), field="latitude", low=Decimal("-90"), high=Decimal("90"))
        lon = self._coordinate(body.get("longitude"), field="longitude", low=Decimal("-180"), high=Decimal("180"))
        recorded_at = _parse_datetime(body.get("recordedAt") or body.get("recorded_at") or _now().isoformat(), "recorded_at")
        if recorded_at and recorded_at > _now() + timedelta(minutes=5):
            raise HTTPException(status_code=422, detail="future_location_timestamp")
        model = MODEL_BY_TABLE["courier_location_updates"]
        row = model(
            user_id=user.id,
            courier_id=getattr(assignment, "courier_id", None) or user.id,
            assignment_id=assignment.id,
            latitude=lat,
            longitude=lon,
            extra_data={
                "accuracy": body.get("accuracy"),
                "provider": body.get("provider") or "device",
                "recorded_at": recorded_at.isoformat() if recorded_at else _now().isoformat(),
                "order_id": str(getattr(assignment, "order_id", "")),
                "assignment_status": assignment.status,
            },
        )
        session.add(row)
        await session.flush()
        payload = serialize_record(row)
        realtime_event = await RealtimeEventService().record_event(
            session,
            channel=f"courier:{user.id}",
            event="courier.location.updated",
            payload=payload,
            dedupe_key=f"courier.location.updated:{row.id}",
            user_id=user.id,
        )
        await realtime_hub.publish_recorded_event(
            f"courier:{user.id}",
            {
                "type": "courier.location.updated",
                "event": "courier.location.updated",
                "payload": payload,
                "event_id": realtime_event.get("event_id") or realtime_event.get("id"),
                "channel": f"courier:{user.id}",
            },
        )
        await session.commit()
        return serialize_record(row)

    async def update_status(self, session: AsyncSession, *, user: User, assignment_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
        assignment_model = MODEL_BY_TABLE["courier_assignments"]
        assignment = (
            await session.execute(
                select(assignment_model)
                .where(
                    assignment_model.id == assignment_id,
                    assignment_model.deleted_at.is_(None),
                    or_(assignment_model.user_id == user.id, assignment_model.courier_id == user.id),
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if assignment is None:
            raise HTTPException(status_code=404, detail="assignment_not_found")
        status = str(body.get("status") or "").strip().lower()
        allowed = {
            "assigned": {"accepted", "cancelled"},
            "active": {"accepted", "picked_up", "out_for_delivery", "cancelled"},
            "accepted": {"picked_up", "out_for_delivery", "failed", "cancelled"},
            "picked_up": {"out_for_delivery", "in_transit", "delivering", "failed"},
            "in_transit": {"out_for_delivery", "delivered", "failed"},
            "delivering": {"out_for_delivery", "delivered", "failed"},
            "out_for_delivery": {"delivered", "failed"},
        }
        current = str(assignment.status or "assigned").lower()
        if status not in set().union(*allowed.values()) or (current in allowed and status not in allowed[current]):
            raise HTTPException(status_code=409, detail="invalid_assignment_status_transition")
        assignment.status = status
        if getattr(assignment, "order_id", None):
            order = await session.get(Order, assignment.order_id)
            if order is not None:
                order_status = {"picked_up": "shipped", "in_transit": "shipped", "delivering": "out_for_delivery", "out_for_delivery": "out_for_delivery", "delivered": "delivered", "failed": "delivery_failed"}.get(status)
                if order_status:
                    order.status = order_status
                    history_model = MODEL_BY_TABLE["order_status_history"]
                    session.add(history_model(order_id=order.id, status=order_status, notes=f"Courier assignment {assignment.id} changed to {status}", extra_data={"changed_by": str(user.id), "assignment_id": str(assignment.id)}))
        await session.commit()
        return serialize_record(assignment)


class ThemeAdminService:
    @staticmethod
    def require_access(roles: set[str]) -> None:
        _require_roles(roles, AUTHORIZED_THEME_ROLES, "theme_permission_denied")

    async def save(self, session: AsyncSession, *, actor: User, roles: set[str], body: dict[str, Any], setting_key: str = "default", publish: bool = True) -> dict[str, Any]:
        self.require_access(roles)
        if not isinstance(body, dict) or not _theme_payload_safe(body):
            raise HTTPException(status_code=422, detail="invalid_theme_payload")
        model = MODEL_BY_TABLE["theme_settings"]
        row = (
            await session.execute(
                select(model).where(model.name == setting_key, model.deleted_at.is_(None)).with_for_update().limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = model(name=setting_key, status="active" if publish else "draft", is_active=True, extra_data={})
            session.add(row)
            await session.flush()
        previous = dict(row.extra_data or {})
        version = int(previous.get("version") or 0) + 1
        row.status = "active" if publish else "draft"
        row.is_active = publish
        row.extra_data = {
            "key": setting_key,
            "value": _jsonable(body.get("value", body)),
            "version": version,
            "published_at": _now().isoformat() if publish else None,
            "updated_by": str(actor.id),
        }
        history = model(
            name=f"history:{setting_key}:{version}",
            status="history",
            is_active=False,
            extra_data={
                "setting_key": setting_key,
                "version": version,
                "description": f"Theme {setting_key} version {version}",
                "old_value": previous.get("value"),
                "new_value": row.extra_data["value"],
                "updated_by": str(actor.id),
            },
        )
        session.add(history)
        session.add(MODEL_BY_TABLE["audit_logs"](user_id=actor.id, type="theme.publish" if publish else "theme.draft", description=f"Updated theme {setting_key}"))
        await session.commit()
        return serialize_record(row)

    async def preview(self, session: AsyncSession, *, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        if not isinstance(body, dict) or not _theme_payload_safe(body):
            raise HTTPException(status_code=422, detail="invalid_theme_payload")
        token = uuid.uuid4().hex
        expires_at = _now() + timedelta(minutes=30)
        model = MODEL_BY_TABLE["theme_settings"]
        row = model(
            name=f"preview:{token}",
            status="preview",
            is_active=False,
            extra_data={"token": token, "value": _jsonable(body.get("value", body)), "expires_at": expires_at.isoformat(), "created_by": str(actor.id)},
        )
        session.add(row)
        await session.commit()
        return {"data": serialize_record(row), "preview_url": f"/api/content/theme/preview/{token}", "expires_at": expires_at.isoformat()}

    async def public_preview(self, session: AsyncSession, *, token: str) -> dict[str, Any]:
        model = MODEL_BY_TABLE["theme_settings"]
        row = (
            await session.execute(
                select(model).where(model.name == f"preview:{token}", model.status == "preview", model.deleted_at.is_(None)).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="preview_not_found")
        expires_at = _parse_datetime((row.extra_data or {}).get("expires_at"), "expires_at")
        if expires_at and expires_at < _now():
            raise HTTPException(status_code=410, detail="preview_expired")
        return serialize_record(row)


class SyncCursorService:
    async def status(self, session: AsyncSession, *, user: User, stream: str, device_id: str, platform: str) -> dict[str, Any]:
        stream = str(stream or "default").strip().lower() or "default"
        platform = str(platform or "unknown").strip().lower() or "unknown"
        device_id = str(device_id or "server").strip() or "server"
        scope = _hash_device(device_id, user.id, stream, platform)
        model = MODEL_BY_TABLE["sync_events"]
        row = (
            await session.execute(
                select(model)
                .where(model.user_id == user.id, model.type == f"sync_cursor:{stream}", model.description == scope, model.deleted_at.is_(None))
                .order_by(model.updated_at.desc())
                .limit(1)
        )
        ).scalar_one_or_none()
        updated_at = row.updated_at if row else None
        revision = int(((row.extra_data if row else {}) or {}).get("revision") or (updated_at.timestamp() if updated_at else 0))
        return {"revision": revision, "updatedAt": updated_at.isoformat() if updated_at else None, "userId": str(user.id), "stream": stream, "deviceHash": scope[:16], "platform": platform}

    async def pull(self, session: AsyncSession, *, user: User, stream: str, body: dict[str, Any]) -> dict[str, Any]:
        device_id = str(body.get("deviceId") or body.get("device_id") or "server")
        platform = str(body.get("platform") or "unknown")
        state = await self.status(session, user=user, stream=stream, device_id=device_id, platform=platform)
        scope = _hash_device(device_id, user.id, stream, platform)
        model = MODEL_BY_TABLE["sync_events"]
        row = (
            await session.execute(
                select(model)
                .where(model.user_id == user.id, model.type == f"sync_cursor:{stream}", model.description == scope, model.deleted_at.is_(None))
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = model(user_id=user.id, type=f"sync_cursor:{stream}", status="active", description=scope, extra_data={})
            session.add(row)
        row.extra_data = {
            **(row.extra_data or {}),
            "revision": int(state["revision"]) + 1,
            "platform": platform,
            "device_hash": scope[:16],
            "last_pull_at": _now().isoformat(),
            "client_cursor": body.get("cursor"),
        }
        await session.commit()
        return {"data": row.extra_data, "cursor": row.extra_data["revision"], "stream": stream}


class SupportWorkflowService:
    @staticmethod
    def _validate_subject_description(subject: str, description: str) -> None:
        normalized_subject = subject.strip().lower()
        normalized_description = description.strip().lower()
        if len(subject.strip()) < 4 or normalized_subject in PLACEHOLDER_TEXT:
            raise HTTPException(status_code=422, detail="support_subject_required")
        if len(description.strip()) < 10 or normalized_description in PLACEHOLDER_TEXT:
            raise HTTPException(status_code=422, detail="support_description_required")

    @staticmethod
    def _can_view(row: Any, user: User, roles: set[str]) -> bool:
        return bool(roles.intersection(SUPPORT_STAFF_ROLES) or row.user_id == user.id)

    async def list(self, session: AsyncSession, *, user: User, roles: set[str], limit: int = 500) -> list[dict[str, Any]]:
        model = MODEL_BY_TABLE["support_tickets"]
        statement = select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(min(limit, 500))
        if not roles.intersection(SUPPORT_STAFF_ROLES):
            statement = statement.where(model.user_id == user.id)
        return [serialize_record(row) for row in (await session.execute(statement)).scalars()]

    async def get(self, session: AsyncSession, *, ticket_id: uuid.UUID, user: User, roles: set[str]) -> Any:
        row = await session.get(MODEL_BY_TABLE["support_tickets"], ticket_id)
        if row is None or row.deleted_at is not None or not self._can_view(row, user, roles):
            raise HTTPException(status_code=404, detail="support_ticket_not_found")
        return row

    async def create(self, session: AsyncSession, *, user: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        subject = str(body.get("subject") or "").strip()
        description = str(body.get("description") or body.get("message") or "").strip()
        self._validate_subject_description(subject, description)
        category = str(body.get("category") or "general").strip().lower()
        priority = str(body.get("priority") or "normal").strip().lower()
        if priority not in {"low", "normal", "high", "urgent"}:
            raise HTTPException(status_code=422, detail="invalid_support_priority")
        now = _now()
        ticket_model = MODEL_BY_TABLE["support_tickets"]
        message_model = MODEL_BY_TABLE["ticket_messages"]
        ticket = ticket_model(
            user_id=user.id,
            subject=subject,
            description=description,
            status="open",
            extra_data={
                "ticket_number": f"SUP-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
                "category": category,
                "priority": priority,
                "workflow": [{"status": "open", "at": now.isoformat(), "by": str(user.id)}],
                "sla": {"first_response_due_at": (now + timedelta(hours=4)).isoformat(), "resolution_due_at": (now + timedelta(days=2)).isoformat(), "breached": False},
            },
        )
        session.add(ticket)
        await session.flush()
        session.add(message_model(ticket_id=ticket.id, sender_id=user.id, message=description, is_staff=bool(roles.intersection(SUPPORT_STAFF_ROLES)), extra_data={"created_from": "ticket_create"}))
        admin_notice = MODEL_BY_TABLE["admin_notifications"]
        session.add(
            admin_notice(
                title="Support ticket opened",
                body=subject,
                message=subject,
                type="support_ticket",
                notification_type="support_ticket",
                category="support",
                priority=priority,
                entity_type="support_ticket",
                entity_id=str(ticket.id),
                payload={"ticket_id": str(ticket.id)},
                status="new",
                is_read=False,
                created_by=user.id,
                source="support",
                deduplication_key=f"support-ticket-opened:{ticket.id}",
                extra_data={"roles": ["admin", "manager", "staff"], "ticket_id": str(ticket.id)},
            )
        )
        await session.commit()
        return serialize_record(ticket)

    async def add_message(self, session: AsyncSession, *, ticket_id: uuid.UUID, user: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        ticket = await self.get(session, ticket_id=ticket_id, user=user, roles=roles)
        message = str(body.get("message") or body.get("body") or "").strip()
        if len(message) < 2 or message.lower() in PLACEHOLDER_TEXT:
            raise HTTPException(status_code=422, detail="support_message_required")
        is_staff = bool(roles.intersection(SUPPORT_STAFF_ROLES))
        model = MODEL_BY_TABLE["ticket_messages"]
        row = model(ticket_id=ticket.id, sender_id=user.id, message=message, is_staff=is_staff, extra_data={"customer_visible": not bool(body.get("internal"))})
        session.add(row)
        extra = dict(ticket.extra_data or {})
        workflow = list(extra.get("workflow") or [])
        workflow.append({"status": "staff_reply" if is_staff else "customer_reply", "at": _now().isoformat(), "by": str(user.id)})
        extra["workflow"] = workflow
        if is_staff:
            sla = dict(extra.get("sla") or {})
            sla.setdefault("first_response_at", _now().isoformat())
            extra["sla"] = sla
            await NotificationService(session).create_notification(
                NotificationPayload(
                    user_id=ticket.user_id,
                    title="Support reply",
                    body=message[:240],
                    notification_type="support_reply",
                    category="support",
                    priority="normal",
                    entity_type="support_ticket",
                    entity_id=str(ticket.id),
                    payload={"ticket_id": str(ticket.id)},
                    created_by=user.id,
                    source="support",
                    deduplication_key=f"support-ticket-reply:{ticket.id}:{row.id}",
                )
            )
        ticket.extra_data = extra
        await session.commit()
        return serialize_record(row)

    async def update_status(self, session: AsyncSession, *, ticket_id: uuid.UUID, user: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        if not roles.intersection(SUPPORT_STAFF_ROLES):
            raise HTTPException(status_code=403, detail="support_status_permission_denied")
        ticket = await self.get(session, ticket_id=ticket_id, user=user, roles=roles)
        status = str(body.get("status") or "").strip().lower()
        status = {"in_progress": "assigned", "waiting_customer": "pending_customer"}.get(status, status)
        allowed = {"open", "assigned", "pending_customer", "resolved", "closed", "reopened"}
        if status not in allowed:
            raise HTTPException(status_code=422, detail="invalid_support_status")
        ticket.status = "open" if status == "reopened" else status
        extra = dict(ticket.extra_data or {})
        workflow = list(extra.get("workflow") or [])
        workflow.append({"status": status, "at": _now().isoformat(), "by": str(user.id), "assigned_to": body.get("assignedTo") or body.get("assigned_to")})
        extra["workflow"] = workflow
        if body.get("assignedTo") or body.get("assigned_to"):
            extra["assigned_to"] = str(body.get("assignedTo") or body.get("assigned_to"))
        ticket.extra_data = extra
        await session.commit()
        return serialize_record(ticket)

    async def delete(self, session: AsyncSession, *, ticket_id: uuid.UUID, user: User, roles: set[str]) -> dict[str, Any]:
        if not roles.intersection({"admin", "manager"}):
            raise HTTPException(status_code=403, detail="support_delete_permission_denied")
        ticket = await self.get(session, ticket_id=ticket_id, user=user, roles=roles)
        ticket.deleted_at = _now()
        extra = dict(ticket.extra_data or {})
        workflow = list(extra.get("workflow") or [])
        workflow.append({"status": "deleted", "at": _now().isoformat(), "by": str(user.id)})
        ticket.extra_data = {**extra, "workflow": workflow}
        await session.commit()
        return {"ok": True, "id": str(ticket.id)}


class OperationalDayService:
    async def today(self, session: AsyncSession) -> dict[str, Any]:
        day = _parse_day(None)
        date_text = day.isoformat()
        model = MODEL_BY_TABLE["operational_days"]
        result = await session.execute(
            select(model)
            .where(
                model.deleted_at.is_(None),
                model.extra_data["date"].astext == date_text,
            )
            .order_by(model.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return {"data": serialize_record(row) if row is not None else None}

    async def action(self, session: AsyncSession, *, actor: User, action: str, raw_date: Any) -> dict[str, Any]:
        day = _parse_day(raw_date)
        date_text = day.isoformat()
        await advisory_xact_lock(session, f"operational-day:{date_text}")
        model = MODEL_BY_TABLE["operational_days"]
        existing = (
            await session.execute(
                select(model)
                .where(model.deleted_at.is_(None), model.extra_data["date"].astext == date_text)
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if action == "open" and existing is not None and existing.status not in {"closed", "reopened"}:
            raise HTTPException(status_code=409, detail="operational_day_already_exists")
        blockers = await self._pending_orders_for_day(session, day)
        if action == "validate":
            return {"data": {"date": date_text, "pending_orders": blockers, "can_close": not blockers}}
        if action == "close" and blockers:
            raise HTTPException(status_code=409, detail={"code": "operational_day_has_pending_orders", "date": date_text, "pending_orders": blockers})
        row = existing
        if row is None:
            row = model(user_id=actor.id, status=action, description=date_text, extra_data={"date": date_text, "workflow": []})
            session.add(row)
        row.status = "closed" if action == "close" else "open"
        workflow = list((row.extra_data or {}).get("workflow") or [])
        workflow.append({"action": action, "at": _now().isoformat(), "by": str(actor.id)})
        row.extra_data = {**(row.extra_data or {}), "date": date_text, "workflow": workflow, "pending_orders_checked": len(blockers)}
        await session.commit()
        return {"data": serialize_record(row)}

    @staticmethod
    async def _pending_orders_for_day(session: AsyncSession, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        result = await session.execute(
            select(Order)
            .where(
                Order.deleted_at.is_(None),
                Order.created_at >= start,
                Order.created_at < end,
                func.lower(Order.status).in_(("pending", "new", "processing", "preparing")),
            )
            .limit(100)
        )
        return [{"id": str(row.id), "order_number": row.order_number, "status": row.status} for row in result.scalars()]


class LoyaltyTierService:
    @staticmethod
    async def list_real_tiers(session: AsyncSession) -> list[dict[str, Any]]:
        model = MODEL_BY_TABLE["loyalty_tiers"]
        result = await session.execute(
            select(model)
            .where(
                model.deleted_at.is_(None),
                model.is_active.is_(True),
                func.lower(model.status).in_(("active", "published", "enabled")),
            )
            .order_by(model.sort_order.asc(), model.created_at.asc())
            .limit(500)
        )
        rows = []
        for row in result.scalars():
            extra = row.extra_data or {}
            if extra.get("demo") is True or extra.get("is_demo") is True or str(extra.get("source") or "").lower() in {"demo", "placeholder", "fixture"}:
                continue
            rows.append(serialize_record(row))
        return rows


class BootstrapVisibilityService:
    @staticmethod
    async def bootstrap(session: AsyncSession, *, user: User) -> dict[str, Any]:
        product_result = await session.execute(
            select(Product).where(*public_product_clauses(Product)).order_by(Product.created_at.desc()).limit(500)
        )
        products = await build_public_product_rows(session, list(product_result.scalars()), include_variants=True)
        categories = [serialize_record(row) for row in (await session.execute(select(MODEL_BY_TABLE["categories"]).where(MODEL_BY_TABLE["categories"].deleted_at.is_(None)).limit(500))).scalars()]
        return {"products": products, "categories": categories, "userId": str(user.id), "visibility": "public_active_approved_only"}


class FormSettingsPersistenceService:
    @staticmethod
    def validate(body: dict[str, Any], *, form_key: str | None = None) -> None:
        key = str(form_key or body.get("form_key") or body.get("formKey") or body.get("name") or "").strip()
        if len(key) < 2:
            raise HTTPException(status_code=422, detail="form_key_required")
        settings = body.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise HTTPException(status_code=422, detail="invalid_form_settings")
        fields = (settings or body).get("fields") if isinstance(settings or body, dict) else None
        if fields is not None and not isinstance(fields, list):
            raise HTTPException(status_code=422, detail="invalid_form_fields")
