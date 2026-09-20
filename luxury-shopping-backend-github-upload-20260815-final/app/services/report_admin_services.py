from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import BACKEND_DIR, get_settings
from ..models import MODEL_BY_TABLE
from ..models.domain import FileAsset, Order, OrderItem, Product, Profile, User, UserRole
from ..repositories.resources import serialize_record
from ..services.catalog_policy import build_public_product_rows, public_product_clauses
from ..services.financial_calculator import advisory_xact_lock, local_request_total, money
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
PENDING_PAYMENT_STATUSES = frozenset(
    {"pending", "unpaid", "awaiting_payment", "under_review", "reviewing", "uploaded", "pending_review"}
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
_THEME_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-f]{3,8}$", re.I)
_THEME_HSL_COLOR_PATTERN = re.compile(
    r"^(?:hsl\(\s*)?(?P<hue>\d{1,3}(?:\.\d+)?)\s+"
    r"(?P<saturation>\d{1,3}(?:\.\d+)?)%\s+"
    r"(?P<lightness>\d{1,3}(?:\.\d+)?)%(?:\s*\))?$",
    re.I,
)
_THEME_CSS_MEASURE_PATTERN = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>px|rem|em|%|ms|s)?$",
    re.I,
)
_THEME_FONT_FAMILIES = frozenset(
    {"Tajawal", "Cairo", "Almarai", "IBM Plex Sans Arabic", "Noto Sans Arabic"}
)
_THEME_SETTING_KEYS = frozenset(
    {"colors", "typography", "layout", "cards", "buttons", "inputs", "animations", "hero"}
)


def _pdf_arabic_font_candidates() -> tuple[Path, ...]:
    configured_path = os.getenv("PDF_ARABIC_FONT_PATH", "").strip()
    candidates = [
        BACKEND_DIR / "assets" / "fonts" / "Tajawal-Regular.ttf",
        BACKEND_DIR.parent / "assets" / "fonts" / "Tajawal-Regular.ttf",
    ]
    if configured_path:
        candidates.insert(0, Path(configured_path).expanduser())
    candidates.extend(
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf"),
            Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        )
    )
    return tuple(dict.fromkeys(candidates))


def _register_pdf_arabic_font(pdfmetrics: Any, ttfont: Any) -> str | None:
    for index, font_path in enumerate(_pdf_arabic_font_candidates()):
        if not font_path.is_file():
            continue
        font_name = f"ArabicReportFont{index}"
        try:
            pdfmetrics.registerFont(ttfont(font_name, str(font_path)))
        except Exception:
            continue
        return font_name
    return None


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


def _invalid_theme_setting(setting_key: str, field: str) -> None:
    raise HTTPException(status_code=422, detail=f"invalid_theme_setting:{setting_key}:{field}")


def _require_theme_mapping(setting_key: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid_theme_setting(setting_key, "value")
    return value


def _validate_theme_color(setting_key: str, field: str, value: Any) -> None:
    text = str(value or "").strip()
    if _THEME_HEX_COLOR_PATTERN.fullmatch(text):
        if len(text) not in {4, 5, 7, 9}:
            _invalid_theme_setting(setting_key, field)
        return
    match = _THEME_HSL_COLOR_PATTERN.fullmatch(text)
    if match is None:
        _invalid_theme_setting(setting_key, field)
    hue = float(match.group("hue"))
    saturation = float(match.group("saturation"))
    lightness = float(match.group("lightness"))
    if hue > 360 or saturation > 100 or lightness > 100:
        _invalid_theme_setting(setting_key, field)


def _validate_theme_measure(
    setting_key: str,
    field: str,
    value: Any,
    *,
    minimum: float,
    maximum: float,
    units: frozenset[str],
) -> None:
    match = _THEME_CSS_MEASURE_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        _invalid_theme_setting(setting_key, field)
    unit = (match.group("unit") or "").lower()
    if unit not in units:
        _invalid_theme_setting(setting_key, field)
    number = float(match.group("number"))
    if not math.isfinite(number) or number < minimum or number > maximum:
        _invalid_theme_setting(setting_key, field)


def _validate_theme_url(setting_key: str, field: str, value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if len(text) > 2048 or not (text.startswith("/") or re.match(r"^https?://", text, re.I)):
        _invalid_theme_setting(setting_key, field)


def _validate_theme_setting_value(setting_key: str, value: Any) -> None:
    """Reject values that can make the published storefront unusable.

    Unknown setting keys stay backward compatible, while every CSS-facing key
    used by the web design panels is checked against the same bounds exposed by
    their controls. A rejected save leaves the last published setting intact.
    """

    if not _theme_payload_safe(value):
        raise HTTPException(status_code=422, detail="invalid_theme_payload")

    key = str(setting_key or "").strip()
    if key == "default":
        data = _require_theme_mapping(key, value)
        for nested_key in _THEME_SETTING_KEYS:
            if nested_key in data:
                _validate_theme_setting_value(nested_key, data[nested_key])
        for field in ("primary", "background", "foreground", "card", "gold", "goldLight", "goldDark"):
            if field in data:
                _validate_theme_color(key, field, data[field])
        if "fontFamily" in data and str(data["fontFamily"]).strip() not in _THEME_FONT_FAMILIES:
            _invalid_theme_setting(key, "fontFamily")
        if "headingSize" in data:
            _validate_theme_measure(
                key,
                "headingSize",
                data["headingSize"],
                minimum=1,
                maximum=4,
                units=frozenset({"", "rem"}),
            )
        if "bodySize" in data:
            _validate_theme_measure(
                key,
                "bodySize",
                data["bodySize"],
                minimum=0.75,
                maximum=1.5,
                units=frozenset({"", "rem"}),
            )
        if "buttonRadius" in data:
            _validate_theme_measure(
                key,
                "buttonRadius",
                data["buttonRadius"],
                minimum=0,
                maximum=32,
                units=frozenset({"", "px"}),
            )
        for field in ("fontScale", "textScale"):
            if field in data:
                _validate_theme_measure(
                    key,
                    field,
                    data[field],
                    minimum=0.5,
                    maximum=2,
                    units=frozenset({""}),
                )
        if "heroImage" in data:
            _validate_theme_url(key, "heroImage", data["heroImage"])
        return
    if key not in _THEME_SETTING_KEYS:
        return
    data = _require_theme_mapping(key, value)

    if key == "colors":
        if not data:
            _invalid_theme_setting(key, "value")
        for field, color in data.items():
            _validate_theme_color(key, str(field), color)
        return

    if key == "typography":
        font_family = data.get("fontFamily")
        if font_family is not None and str(font_family).strip() not in _THEME_FONT_FAMILIES:
            _invalid_theme_setting(key, "fontFamily")
        if "headingSize" in data:
            _validate_theme_measure(key, "headingSize", data["headingSize"], minimum=1, maximum=4, units=frozenset({"", "rem"}))
        if "bodySize" in data:
            _validate_theme_measure(key, "bodySize", data["bodySize"], minimum=0.75, maximum=1.5, units=frozenset({"", "rem"}))
        return

    if key == "layout":
        if "containerWidth" in data:
            _validate_theme_measure(key, "containerWidth", data["containerWidth"], minimum=1000, maximum=1920, units=frozenset({"px"}))
        if "sectionPadding" in data:
            _validate_theme_measure(key, "sectionPadding", data["sectionPadding"], minimum=2, maximum=12, units=frozenset({"rem"}))
        if "borderRadius" in data:
            _validate_theme_measure(key, "borderRadius", data["borderRadius"], minimum=0, maximum=2, units=frozenset({"rem"}))
        if data.get("headerStyle") is not None and data.get("headerStyle") not in {"fixed", "sticky", "static"}:
            _invalid_theme_setting(key, "headerStyle")
        return

    if key in {"cards", "buttons", "inputs"}:
        radius_key = "inputRadius" if key == "inputs" else f"{key[:-1]}Radius"
        radius = data.get("borderRadius", data.get(radius_key))
        if radius is not None:
            _validate_theme_measure(key, "borderRadius", radius, minimum=0, maximum=2, units=frozenset({"", "rem"}))
        if key == "cards":
            shadow = data.get("shadow", data.get("cardShadow"))
            if shadow is not None and shadow not in {"none", "sm", "md", "lg", "elegant", "dramatic"}:
                _invalid_theme_setting(key, "shadow")
            hover = data.get("hoverEffect", data.get("cardHover"))
            if hover is not None and not isinstance(hover, bool):
                _invalid_theme_setting(key, "hoverEffect")
        if key == "buttons":
            size = data.get("size", data.get("buttonSize"))
            if size is not None and size not in {"compact", "default", "large"}:
                _invalid_theme_setting(key, "size")
        return

    if key == "animations":
        if data.get("enabled") is not None and not isinstance(data["enabled"], bool):
            _invalid_theme_setting(key, "enabled")
        if "duration" in data:
            _validate_theme_measure(key, "duration", data["duration"], minimum=0, maximum=1, units=frozenset({"s"}))
        if data.get("type") is not None and data.get("type") not in {"none", "fade", "slide", "scale", "spring"}:
            _invalid_theme_setting(key, "type")
        return

    if key == "hero":
        for field, maximum in (("title", 160), ("subtitle", 500)):
            if field in data and (not isinstance(data[field], str) or len(data[field].strip()) > maximum):
                _invalid_theme_setting(key, field)
        image_value = data.get("imageUrl", data.get("image_url"))
        if image_value is not None:
            _validate_theme_url(key, "imageUrl", image_value)
        if data.get("showCta") is not None and not isinstance(data["showCta"], bool):
            _invalid_theme_setting(key, "showCta")


def _validate_theme_settings_collection(value: Any) -> None:
    data = _require_theme_mapping("preview", value)
    for key, item in data.items():
        if key == "components":
            components = _require_theme_mapping("components", item)
            card_value = {
                name: components[name]
                for name in ("cardRadius", "cardShadow", "cardHover")
                if name in components
            }
            button_value = {
                name: components[name]
                for name in ("buttonRadius", "buttonSize")
                if name in components
            }
            input_value = {"inputRadius": components["inputRadius"]} if "inputRadius" in components else {}
            if card_value:
                _validate_theme_setting_value("cards", card_value)
            if button_value:
                _validate_theme_setting_value("buttons", button_value)
            if input_value:
                _validate_theme_setting_value("inputs", input_value)
            continue
        _validate_theme_setting_value(str(key), item)


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
    async def _regular_order_rows(
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
        # The checkout flow records verified payments in ``payments`` while
        # manual/admin settlement flows record them in ``order_payments``.
        # Finance orders already reconciles both sources; accounting must use
        # the same ledger or its recognised revenue can incorrectly be zero.
        refund_model = MODEL_BY_TABLE["refunds"]
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
        for table_name in ("order_payments", "payments"):
            payment_model = MODEL_BY_TABLE[table_name]
            payments_result = await session.execute(
                select(payment_model)
                .where(
                    payment_model.order_id.in_(order_ids),
                    payment_model.deleted_at.is_(None),
                    func.lower(payment_model.status).in_(tuple(RECOGNIZED_PAYMENT_STATUSES)),
                )
            )
            for payment in payments_result.scalars():
                payments_by_order[payment.order_id] = money(
                    payments_by_order.get(payment.order_id, 0) + money(payment.amount or 0)
                )
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

    @staticmethod
    async def _supplemental_orders(
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
    ) -> list[tuple[str, Any]]:
        """Load local and international orders that use separate ledgers."""

        start_dt, end_dt = _date_range(start, end)
        records: list[tuple[str, Any]] = []
        for table in ("local_shopping_requests", "international_orders"):
            model = MODEL_BY_TABLE[table]
            clauses = [
                model.deleted_at.is_(None),
                ~func.lower(model.status).in_(tuple(EXCLUDED_ORDER_STATUSES)),
            ]
            if start_dt is not None:
                clauses.append(model.created_at >= start_dt)
            if end_dt is not None:
                clauses.append(model.created_at <= end_dt)
            result = await session.execute(select(model).where(*clauses).order_by(model.created_at.desc()))
            records.extend((table, record) for record in result.scalars())
        return records

    @staticmethod
    def _supplemental_payload_total(payload: dict[str, Any]) -> Decimal:
        """Resolve the customer-facing amount from compatibility payload fields."""

        for field in ("final_cost", "finalCost", "estimated_cost", "estimatedCost"):
            candidate = payload.get(field)
            try:
                resolved = money(candidate or 0)
            except HTTPException:
                resolved = Decimal("0.00")
            if resolved > 0:
                return resolved
        local_total = local_request_total(payload)
        if local_total > 0:
            return local_total
        for field in (
            "shipping_cost",
            "shippingCost",
            "service_fee",
            "serviceFee",
            "customs_cost",
            "customsCost",
            "amount",
            "total",
        ):
            candidate = payload.get(field)
            try:
                resolved = money(candidate or 0)
            except HTTPException:
                resolved = Decimal("0.00")
            if resolved > 0:
                return resolved
        return Decimal("0.00")

    @classmethod
    async def _supplemental_order_rows(
        cls,
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
        partner_id: uuid.UUID | None = None,
    ) -> list[RecognizedOrderRevenue]:
        if partner_id is not None:
            return []

        records = await cls._supplemental_orders(session, start=start, end=end)
        if not records:
            return []

        local_records = [record for table, record in records if table == "local_shopping_requests"]
        international_records = [record for table, record in records if table == "international_orders"]
        paid_by_local: dict[str, Decimal] = {}
        if local_records:
            payment_model = MODEL_BY_TABLE["order_payments"]
            local_request_id = payment_model.extra_data.op("->>")(literal_column("'local_request_id'"))
            result = await session.execute(
                select(local_request_id, func.coalesce(func.sum(payment_model.amount), 0))
                .where(
                    payment_model.deleted_at.is_(None),
                    local_request_id.in_([str(record.id) for record in local_records]),
                    func.lower(payment_model.status).in_(tuple(RECOGNIZED_PAYMENT_STATUSES)),
                )
                .group_by(local_request_id)
            )
            paid_by_local = {
                str(request_id): money(amount or 0)
                for request_id, amount in result.all()
                if request_id
            }

        paid_by_international: dict[str, Decimal] = {}
        if international_records:
            payment_model = MODEL_BY_TABLE["international_order_payments"]
            result = await session.execute(
                select(payment_model.order_id, func.coalesce(func.sum(payment_model.amount), 0))
                .where(
                    payment_model.deleted_at.is_(None),
                    payment_model.order_id.in_([record.id for record in international_records]),
                    func.lower(payment_model.status).in_(tuple(RECOGNIZED_PAYMENT_STATUSES)),
                )
                .group_by(payment_model.order_id)
            )
            paid_by_international = {
                str(order_id): money(amount or 0)
                for order_id, amount in result.all()
                if order_id
            }

        rows: list[RecognizedOrderRevenue] = []
        for table, record in records:
            payload = serialize_record(record)
            order_total = cls._supplemental_payload_total(payload)
            payment_total = (
                paid_by_local.get(str(record.id), Decimal("0.00"))
                if table == "local_shopping_requests"
                else paid_by_international.get(str(record.id), Decimal("0.00"))
            )
            payment_status = str(
                payload.get("payment_status") or payload.get("paymentStatus") or ""
            ).strip().lower()
            if payment_total <= 0 and payment_status in RECOGNIZED_PAYMENT_STATUSES:
                payment_total = order_total
            if payment_total <= 0:
                continue
            if order_total <= 0:
                # A legacy record can contain a payment without a quoted total;
                # the ledger amount is the only safe amount available then.
                order_total = payment_total
            order_number = str(
                payload.get("order_number")
                or payload.get("orderNumber")
                or f"{'LS' if table == 'local_shopping_requests' else 'IO'}-{str(record.id)[:8].upper()}"
            )
            currency_code = str(
                payload.get("currency_code")
                or payload.get("currencyCode")
                or "YER"
            )
            rows.append(
                RecognizedOrderRevenue(
                    order_id=record.id,
                    order_number=order_number,
                    order_total=order_total,
                    partner_share_gross=order_total,
                    payment_total=payment_total,
                    refund_total=Decimal("0.00"),
                    net_revenue=money(min(order_total, payment_total)),
                    currency_code=currency_code,
                    status=str(getattr(record, "status", "") or ""),
                    payment_status=payment_status or "paid",
                )
            )
        return rows

    @classmethod
    async def order_rows(
        cls,
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
        partner_id: uuid.UUID | None = None,
    ) -> list[RecognizedOrderRevenue]:
        regular_rows = await cls._regular_order_rows(
            session,
            start=start,
            end=end,
            partner_id=partner_id,
        )
        supplemental_rows = await cls._supplemental_order_rows(
            session,
            start=start,
            end=end,
            partner_id=partner_id,
        )
        return regular_rows + supplemental_rows

    @classmethod
    async def order_activity_summary(
        cls,
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
    ) -> dict[str, Any]:
        """Return the value and count of every eligible order, paid or not.

        Revenue recognition intentionally counts only successful payments. The
        accounting dashboard also needs the order activity itself so pending
        cash-on-delivery and local/international requests are visible instead
        of making a busy period look empty.
        """

        orders = await cls.eligible_orders(session, start=start, end=end)
        supplemental = await cls._supplemental_orders(session, start=start, end=end)
        order_value = money(sum((money(order.total or 0) for order in orders), Decimal("0.00")))
        currency_code = next(
            (
                str(getattr(order, "currency_code", "") or "").strip() or "YER"
                for order in orders
            ),
            "YER",
        )
        for _, record in supplemental:
            payload = serialize_record(record)
            order_value += cls._supplemental_payload_total(payload)
            if currency_code == "YER":
                currency_code = str(
                    payload.get("currency_code")
                    or payload.get("currencyCode")
                    or "YER"
                ).strip() or "YER"
        return {
            "order_count": len(orders) + len(supplemental),
            "order_value": money(order_value),
            "currency_code": currency_code,
        }

    @classmethod
    async def pending_payment_amount(
        cls,
        session: AsyncSession,
        *,
        start: Any = None,
        end: Any = None,
    ) -> Decimal:
        """Return outstanding order balances plus unreviewed payment receipts."""

        paid_by_order: dict[str, Decimal] = {}
        for row in await cls.order_rows(session, start=start, end=end):
            key = str(row.order_id)
            paid_by_order[key] = money(paid_by_order.get(key, 0) + row.payment_total)

        outstanding = Decimal("0.00")
        for order in await cls.eligible_orders(session, start=start, end=end):
            total = money(order.total or 0)
            outstanding += max(total - paid_by_order.get(str(order.id), Decimal("0.00")), Decimal("0.00"))

        for _, record in await cls._supplemental_orders(session, start=start, end=end):
            payload = serialize_record(record)
            total = cls._supplemental_payload_total(payload)
            outstanding += max(total - paid_by_order.get(str(record.id), Decimal("0.00")), Decimal("0.00"))

        start_dt, end_dt = _date_range(start, end)
        receipt_model = MODEL_BY_TABLE["payment_receipts"]
        receipt_clauses = [
            receipt_model.deleted_at.is_(None),
            # Order-linked receipts are already represented by the order's
            # outstanding balance; only standalone receipts (for example a
            # merchant subscription) belong in this extra total.
            receipt_model.order_id.is_(None),
            func.lower(receipt_model.status).in_(tuple(PENDING_PAYMENT_STATUSES)),
        ]
        if start_dt is not None:
            receipt_clauses.append(receipt_model.created_at >= start_dt)
        if end_dt is not None:
            receipt_clauses.append(receipt_model.created_at <= end_dt)
        receipt_total = await session.execute(
            select(func.coalesce(func.sum(receipt_model.amount), 0)).where(*receipt_clauses)
        )
        return money(outstanding + money(receipt_total.scalar_one() or 0))

    @classmethod
    async def summary(cls, session: AsyncSession, *, start: Any = None, end: Any = None, partner_id: uuid.UUID | None = None) -> dict[str, Any]:
        rows = await cls.order_rows(session, start=start, end=end, partner_id=partner_id)
        activity = await cls.order_activity_summary(session, start=start, end=end)
        gross = money(sum((row.partner_share_gross for row in rows), Decimal("0.00")))
        refunds = money(sum((row.refund_total for row in rows), Decimal("0.00")))
        net = money(sum((row.net_revenue for row in rows), Decimal("0.00")))
        paid = money(sum((row.payment_total for row in rows), Decimal("0.00")))
        return {
            "date_basis": "order records created_at plus successful payment/refund status",
            "order_count": len(rows),
            "eligible_order_count": len(rows),
            "gross_revenue": format(gross, "f"),
            "paid_amount": format(paid, "f"),
            "refund_amount": format(refunds, "f"),
            "net_revenue": format(net, "f"),
            "currency_code": rows[0].currency_code if rows else "YER",
            "partner_scope": str(partner_id) if partner_id else None,
            "order_activity_count": activity["order_count"],
            "order_activity_value": format(activity["order_value"], "f"),
            "order_activity_currency_code": activity["currency_code"],
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
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.colors import HexColor, white
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import ImageReader
            from arabic_reshaper import ArabicReshaper
            from bidi.algorithm import get_display
        except Exception as exc:
            raise HTTPException(status_code=503, detail="pdf_renderer_unavailable") from exc
        arabic_text_reshaper = ArabicReshaper({"use_unshaped_instead_of_isolated": True})
        font_name = _register_pdf_arabic_font(pdfmetrics, TTFont) or "Helvetica"
        buffer = io.BytesIO()
        page_size = landscape(A4) if report_type in {"sales", "orders", "customers", "merchant_revenue"} else A4
        doc = canvas.Canvas(buffer, pagesize=page_size)
        width, height = page_size
        if font_name == "Helvetica":
            raise HTTPException(status_code=503, detail="pdf_arabic_font_unavailable")

        logo_path = BACKEND_DIR / "assets" / "branding" / "luxury-shopping-logo.png"
        if not logo_path.is_file():
            raise HTTPException(status_code=503, detail="pdf_brand_logo_unavailable")
        try:
            brand_logo = ImageReader(str(logo_path))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="pdf_brand_logo_unavailable") from exc

        charcoal = HexColor("#1C1917")
        charcoal_soft = HexColor("#44403C")
        brand_gold = HexColor("#A16207")
        warm_white = HexColor("#FAFAF9")
        soft_surface = HexColor("#F5F5F4")
        border_color = HexColor("#D6D3D1")
        muted_text = HexColor("#57534E")
        ink = HexColor("#0C0A09")
        generated_at = _now()
        generated_label = generated_at.strftime("%Y/%m/%d - %H:%M UTC")
        report_reference = generated_at.strftime("RPT-%Y%m%d-%H%M%S")
        report_titles = {
            "summary": ("الملخص المالي", "ملخص مؤشرات الإيرادات والمدفوعات"),
            "sales": ("تقرير المبيعات", "تفاصيل الطلبات والإيرادات"),
            "orders": ("تقرير الطلبات", "حالات الطلبات والتحصيل المالي"),
            "revenue": ("تقرير الإيرادات", "تحليل الإيرادات للفترة المحددة"),
            "customers": ("تقرير العملاء", "ملخص العملاء والإنفاق للفترة المحددة"),
            "merchant_revenue": ("تقرير إيرادات التاجر", "ملخص مستحقات التاجر للفترة المحددة"),
        }
        report_title, report_subtitle = report_titles.get(
            report_type,
            ("التقرير المالي", "تقرير مالي رسمي"),
        )

        def rtl(value: Any) -> str:
            return get_display(arabic_text_reshaper.reshape(str(value)))

        def draw_page_background() -> None:
            doc.setFillColor(warm_white)
            doc.rect(0, 0, width, height, fill=1, stroke=0)

        def draw_brand_header(margin: float) -> None:
            right = width - margin
            content_width = width - (margin * 2)
            header_y = height - 118
            doc.setFillColor(charcoal)
            doc.roundRect(margin, header_y, content_width, 84, 12, fill=1, stroke=0)
            doc.setFillColor(brand_gold)
            doc.roundRect(right - 7, header_y, 7, 84, 3, fill=1, stroke=0)
            doc.drawImage(
                brand_logo,
                margin + 17,
                header_y + 12,
                width=60,
                height=60,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
            doc.setFillColor(HexColor("#E8C66A"))
            doc.setFont(font_name, 9)
            doc.drawRightString(right - 22, header_y + 62, rtl("رفاهية التسوق"))
            doc.setFillColor(white)
            doc.setFont(font_name, 18)
            doc.drawRightString(right - 22, header_y + 39, rtl(report_title))
            doc.setFillColor(HexColor("#E7E5E4"))
            doc.setFont(font_name, 9)
            doc.drawRightString(right - 22, header_y + 19, rtl(report_subtitle))
            doc.setStrokeColor(brand_gold)
            doc.setLineWidth(1.2)
            doc.line(margin, header_y - 12, right, header_y - 12)
            doc.setFillColor(muted_text)
            doc.setFont(font_name, 7.5)
            doc.drawRightString(right, header_y - 27, rtl(f"تاريخ الإنشاء: {generated_label}"))
            doc.drawString(margin, header_y - 27, report_reference)

        def draw_report_footer(margin: float, page_number: int) -> None:
            right = width - margin
            doc.setStrokeColor(brand_gold)
            doc.setLineWidth(0.7)
            doc.line(margin, 34, right, 34)
            doc.setFillColor(muted_text)
            doc.setFont(font_name, 7.5)
            doc.drawRightString(right, 20, rtl("رفاهية التسوق - تقرير رسمي"))
            doc.drawCentredString(width / 2, 20, report_reference)
            doc.drawString(margin, 20, rtl(f"صفحة {page_number}"))

        doc.setTitle(f"رفاهية التسوق - {report_title}")
        doc.setAuthor("رفاهية التسوق")
        doc.setCreator("رفاهية التسوق - المركز المحاسبي")
        doc.setSubject(report_subtitle)

        if report_type in {"sales", "orders"}:
            def amount(value: Any) -> str:
                try:
                    return f"{Decimal(str(value)).quantize(Decimal('0.01')):,.2f} ريال يمني"
                except (InvalidOperation, ValueError):
                    return "-"

            def count(value: Any) -> str:
                try:
                    return f"{int(Decimal(str(value or 0))):,}"
                except (InvalidOperation, ValueError):
                    return "0"

            order_status_labels = {
                "accepted": "مقبول",
                "cancelled": "ملغى",
                "completed": "مكتمل",
                "confirmed": "مؤكد",
                "delivered": "تم التسليم",
                "new": "جديد",
                "out_for_delivery": "قيد التوصيل",
                "pending": "قيد الانتظار",
                "processing": "قيد المعالجة",
                "ready_for_shipment": "جاهز للشحن",
                "rejected": "مرفوض",
                "shipped": "تم الشحن",
                "under_review": "قيد المراجعة",
            }
            payment_status_labels = {
                "cancelled": "ملغى",
                "failed": "فشل الدفع",
                "paid": "مدفوع",
                "partially_paid": "مدفوع جزئيًا",
                "pending": "قيد الانتظار",
                "refunded": "مسترد",
                "unpaid": "غير مدفوع",
            }

            def status_label(value: Any, labels: dict[str, str]) -> str:
                normalized = str(value or "").strip().lower()
                return labels.get(normalized, "غير محدد")

            margin = 30
            right = width - margin
            content_width = width - (margin * 2)
            draw_page_background()
            draw_brand_header(margin)

            summary_cards = (
                ("إجمالي الإيرادات", amount(metadata.get("gross_revenue"))),
                ("إجمالي المبالغ المدفوعة", amount(metadata.get("paid_amount"))),
                ("صافي الإيرادات", amount(metadata.get("net_revenue"))),
                ("عدد الطلبات", count(metadata.get("order_count"))),
            )
            card_gap = 12
            card_width = (content_width - (card_gap * 3)) / 4
            card_y = height - 210
            for index, (label, value) in enumerate(summary_cards):
                card_x = margin + index * (card_width + card_gap)
                doc.setFillColor(soft_surface)
                doc.setStrokeColor(border_color)
                doc.roundRect(card_x, card_y, card_width, 48, 7, fill=1, stroke=1)
                doc.setFillColor(muted_text)
                doc.setFont(font_name, 8)
                doc.drawCentredString(card_x + card_width / 2, card_y + 31, rtl(label))
                doc.setFillColor(ink)
                doc.setFont(font_name, 9)
                doc.drawCentredString(card_x + card_width / 2, card_y + 14, rtl(value))

            doc.setFillColor(charcoal_soft)
            doc.setFont(font_name, 9)
            doc.drawRightString(
                right,
                height - 230,
                rtl("أساس التقرير: تاريخ إنشاء الطلب مع احتساب الدفع والاسترداد الناجحين"),
            )
            doc.setStrokeColor(border_color)
            doc.line(margin, height - 240, right, height - 240)

            table_columns = (
                ("net", "الصافي", "amount", 100),
                ("refunds", "المسترد", "amount", 100),
                ("paid", "المدفوع", "amount", 100),
                ("gross", "الإجمالي", "amount", 100),
                ("payment_status", "حالة الدفع", "payment_status", 104),
                ("status", "حالة الطلب", "status", 112),
                ("order_number", "رقم الطلب", "order_number", 138),
                ("rank", "#", "rank", 28),
            )
            row_height = 26

            def draw_table_header(table_y: float) -> float:
                x = margin
                doc.setFillColor(charcoal)
                doc.setStrokeColor(charcoal)
                doc.setFont(font_name, 8)
                for _, label, _, column_width in table_columns:
                    doc.rect(x, table_y - row_height, column_width, row_height, fill=1, stroke=1)
                    doc.setFillColor(white)
                    doc.drawCentredString(x + column_width / 2, table_y - 17, rtl(label))
                    doc.setFillColor(charcoal)
                    x += column_width
                return table_y - row_height

            def draw_footer(page_number: int) -> None:
                draw_report_footer(margin, page_number)

            y = draw_table_header(height - 254)
            page_number = 1
            for index, row in enumerate(rows, start=1):
                if y - row_height < 46:
                    draw_footer(page_number)
                    doc.showPage()
                    page_number += 1
                    draw_page_background()
                    draw_brand_header(margin)
                    y = draw_table_header(height - 174)
                x = margin
                for key, _, kind, column_width in table_columns:
                    if index % 2 == 1:
                        doc.setFillColor(soft_surface)
                    else:
                        doc.setFillColor(warm_white)
                    doc.rect(x, y - row_height, column_width, row_height, fill=1, stroke=0)
                    if kind == "amount":
                        value = rtl(amount(row.get(key)))
                    elif kind == "payment_status":
                        value = rtl(status_label(row.get(key), payment_status_labels))
                    elif kind == "status":
                        value = rtl(status_label(row.get(key), order_status_labels))
                    elif kind == "order_number":
                        value = str(row.get(key) or row.get("order_id") or "-")
                    else:
                        value = str(index)
                    doc.setFillColor(ink)
                    doc.setFont(font_name, 8 if kind == "order_number" else 9)
                    doc.drawCentredString(x + column_width / 2, y - 17, value)
                    x += column_width
                y -= row_height
            draw_footer(page_number)
            doc.save()
            return buffer.getvalue()
        if report_type == "summary":
            def amount(value: Any) -> str:
                try:
                    return f"{Decimal(str(value)).quantize(Decimal('0.01')):,.2f} ريال يمني"
                except (InvalidOperation, ValueError):
                    return _safe_csv_cell(value)

            def summary_value(key: str, value: Any) -> str:
                if key in {"order_count", "eligible_order_count"}:
                    try:
                        return f"{int(Decimal(str(value or 0))):,}"
                    except (InvalidOperation, ValueError):
                        return _safe_csv_cell(value)
                if key in {"gross_revenue", "paid_amount", "refund_amount", "net_revenue"}:
                    return amount(value)
                if key == "currency_code":
                    return "ريال يمني" if str(value or "").upper() == "YER" else _safe_csv_cell(value)
                if key == "date_basis":
                    return "تاريخ إنشاء الطلب مع احتساب الدفع والاسترداد الناجحين"
                if key == "partner_scope":
                    return "كافة الشركاء" if not value else str(value)
                return _safe_csv_cell(value) or "—"

            labels = {
                "date_basis": "أساس احتساب التاريخ",
                "order_count": "عدد الطلبات",
                "eligible_order_count": "عدد الطلبات المؤهلة",
                "gross_revenue": "إجمالي الإيرادات",
                "paid_amount": "إجمالي المبالغ المدفوعة",
                "refund_amount": "إجمالي المبالغ المستردة",
                "net_revenue": "صافي الإيرادات",
                "currency_code": "العملة",
                "partner_scope": "نطاق الشركاء",
            }
            margin = 44
            right = width - margin
            content_width = width - (margin * 2)
            page_number = 1
            draw_page_background()
            draw_brand_header(margin)

            y = height - 174
            doc.setFillColor(charcoal)
            doc.setFont(font_name, 12)
            doc.drawRightString(right, y, rtl("ملخص التقرير"))
            y -= 14
            doc.setStrokeColor(brand_gold)
            doc.line(margin, y, right, y)
            y -= 22
            doc.setFont(font_name, 10)
            for index, (key, value) in enumerate(metadata.items()):
                if y < 86:
                    draw_report_footer(margin, page_number)
                    doc.showPage()
                    page_number += 1
                    draw_page_background()
                    draw_brand_header(margin)
                    doc.setFont(font_name, 10)
                    y = height - 174
                if index % 2 == 0:
                    doc.setFillColor(soft_surface)
                    doc.roundRect(margin, y - 8, content_width, 28, 5, fill=1, stroke=0)
                doc.setFillColor(charcoal_soft)
                doc.drawRightString(right - 12, y, rtl(labels.get(key, key)))
                doc.setFillColor(ink)
                doc.drawString(margin + 12, y, rtl(summary_value(key, value)))
                y -= 34
            draw_report_footer(margin, page_number)
            doc.save()
            return buffer.getvalue()

        def localized_value(key: str, value: Any) -> str:
            if value is None or str(value).strip() == "":
                return "—"
            if key in {"orders", "order_count", "eligible_order_count", "quantity"}:
                try:
                    return f"{int(Decimal(str(value))):,}"
                except (InvalidOperation, ValueError):
                    return _safe_csv_cell(value)
            if key in {
                "gross_revenue",
                "paid_amount",
                "refund_amount",
                "net_revenue",
                "gross",
                "paid",
                "refunds",
                "net",
                "merchant_gross",
                "total_spent",
                "total_price",
                "revenue",
            }:
                try:
                    return f"{Decimal(str(value)).quantize(Decimal('0.01')):,.2f} ريال يمني"
                except (InvalidOperation, ValueError):
                    return _safe_csv_cell(value)
            if key == "currency_code":
                return "ريال يمني" if str(value).upper() == "YER" else _safe_csv_cell(value)
            if key == "date_basis":
                return "تاريخ إنشاء الطلب مع احتساب الدفع والاسترداد الناجحين"
            if key == "partner_scope":
                return "كافة الشركاء" if not value else _safe_csv_cell(value)
            if key in {"status", "payment_status", "classification"}:
                labels = {
                    "accepted": "مقبول",
                    "cancelled": "ملغى",
                    "completed": "مكتمل",
                    "confirmed": "مؤكد",
                    "delivered": "تم التسليم",
                    "failed": "فشل",
                    "normal": "عادي",
                    "paid": "مدفوع",
                    "pending": "قيد الانتظار",
                    "processing": "قيد المعالجة",
                    "ready_for_shipment": "جاهز للشحن",
                    "rejected": "مرفوض",
                    "refunded": "مسترد",
                    "vip": "مميز",
                }
                return labels.get(str(value).strip().lower(), _safe_csv_cell(value))
            return _safe_csv_cell(value)

        labels = {
            "date_basis": "أساس احتساب التاريخ",
            "order_count": "عدد الطلبات",
            "eligible_order_count": "عدد الطلبات المؤهلة",
            "gross_revenue": "إجمالي الإيرادات",
            "paid_amount": "إجمالي المبالغ المدفوعة",
            "refund_amount": "إجمالي المبالغ المستردة",
            "net_revenue": "صافي الإيرادات",
            "currency_code": "العملة",
            "partner_scope": "نطاق الشركاء",
            "order_id": "معرّف الطلب",
            "order_number": "رقم الطلب",
            "status": "حالة الطلب",
            "payment_status": "حالة الدفع",
            "gross": "الإجمالي",
            "paid": "المدفوع",
            "refunds": "المسترد",
            "net": "الصافي",
            "customer_id": "معرّف العميل",
            "name": "اسم العميل",
            "email": "البريد الإلكتروني",
            "classification": "التصنيف",
            "created_at": "تاريخ الإنشاء",
            "orders": "عدد الطلبات",
            "total_spent": "إجمالي الإنفاق",
            "merchant_gross": "إجمالي التاجر",
            "revenue": "الإيراد",
            "quantity": "الكمية",
            "total_price": "الإجمالي",
            "metric": "المؤشر",
            "value": "القيمة",
        }
        margin = 36
        right = width - margin
        content_width = width - (margin * 2)
        page_number = 1
        draw_page_background()
        draw_brand_header(margin)
        y = height - 174
        doc.setFillColor(charcoal)
        doc.setFont(font_name, 12)
        doc.drawRightString(right, y, rtl("ملخص التقرير"))
        y -= 16
        doc.setStrokeColor(brand_gold)
        doc.line(margin, y, right, y)
        y -= 24
        doc.setFont(font_name, 9)
        for index, (key, value) in enumerate(metadata.items()):
            if y < 86:
                draw_report_footer(margin, page_number)
                doc.showPage()
                page_number += 1
                draw_page_background()
                draw_brand_header(margin)
                y = height - 174
                doc.setFont(font_name, 9)
            if index % 2 == 0:
                doc.setFillColor(soft_surface)
                doc.roundRect(margin, y - 8, content_width, 28, 5, fill=1, stroke=0)
            doc.setFillColor(charcoal_soft)
            doc.drawRightString(right - 12, y, rtl(labels.get(key, key)))
            doc.setFillColor(ink)
            doc.drawString(margin + 12, y, rtl(localized_value(key, value)))
            y -= 34

        if rows:
            if y < 100:
                draw_report_footer(margin, page_number)
                doc.showPage()
                page_number += 1
                draw_page_background()
                draw_brand_header(margin)
                y = height - 174
            y -= 8
            doc.setFillColor(charcoal)
            doc.setFont(font_name, 12)
            doc.drawRightString(right, y, rtl("تفاصيل التقرير"))
            doc.setStrokeColor(brand_gold)
            doc.line(margin, y - 8, right, y - 8)
            y -= 20
            for row_index, row in enumerate(rows, start=1):
                block_height = 24 + (len(columns) * 18)
                if y - block_height < 46:
                    draw_report_footer(margin, page_number)
                    doc.showPage()
                    page_number += 1
                    draw_page_background()
                    draw_brand_header(margin)
                    y = height - 174
                if row_index % 2 == 1:
                    doc.setFillColor(soft_surface)
                    doc.roundRect(margin, y - block_height + 8, content_width, block_height, 6, fill=1, stroke=0)
                line_y = y - 10
                for key in columns:
                    doc.setFillColor(charcoal_soft)
                    doc.setFont(font_name, 8)
                    doc.drawRightString(right - 12, line_y, rtl(labels.get(key, key)))
                    doc.setFillColor(ink)
                    doc.setFont(font_name, 8)
                    doc.drawString(margin + 12, line_y, rtl(localized_value(key, row.get(key))))
                    line_y -= 18
                y -= block_height
        draw_report_footer(margin, page_number)
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
        customer_user_ids = select(UserRole.user_id).where(UserRole.role == "customer")
        result = await session.execute(
            select(User, Profile)
            .outerjoin(Profile, Profile.user_id == User.id)
            .where(User.deleted_at.is_(None), User.id.in_(customer_user_ids))
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        user_ids = []
        rows = []
        for user, profile in result.all():
            user_ids.append(user.id)
            profile_extra = dict(profile.extra_data or {}) if profile else {}
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
                # Keep the flat fields consumed by the accounting statement
                # selector while retaining the nested profile contract used
                # by the customer-management screen.
                "full_name": profile.full_name if profile else None,
                "phone": profile.phone if profile else None,
                "city": profile.city if profile else None,
                "governorate": profile_extra.get("governorate"),
            }
            if full:
                row["roles"] = []
                row["profile"].update(
                    {
                        "avatar_url": profile.avatar_url if profile else None,
                        "classification": profile.classification if profile else None,
                        "admin_notes": profile_extra.get("admin_notes"),
                    }
                )
            rows.append(row)
        if user_ids:
            address_model = MODEL_BY_TABLE["customer_addresses"]
            address_result = await session.execute(
                select(address_model)
                .where(
                    address_model.deleted_at.is_(None),
                    address_model.user_id.in_(user_ids),
                )
                .order_by(address_model.is_default.desc(), address_model.updated_at.desc())
            )
            addresses_by_user: dict[uuid.UUID, Any] = {}
            for address in address_result.scalars():
                addresses_by_user.setdefault(address.user_id, address)
            for row in rows:
                address = addresses_by_user.get(uuid.UUID(row["user_id"]))
                if address is None:
                    continue
                address_city = str(getattr(address, "city", None) or "").strip()
                address_governorate = str(getattr(address, "governorate", None) or "").strip()
                row["city"] = row["city"] or address_city or address_governorate or None
                row["governorate"] = row["governorate"] or address_governorate or None
                row["profile"]["city"] = row["profile"]["city"] or row["city"]

            # The customer-management UI derives its order cards from these
            # fields. Keep the aggregation on the canonical orders table so
            # it follows the same eligibility rules as admin reports.
            order_stats_result = await session.execute(
                select(
                    Order.user_id,
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.total), 0),
                    func.max(Order.created_at),
                )
                .where(
                    Order.deleted_at.is_(None),
                    Order.user_id.in_(user_ids),
                    ~func.lower(Order.status).in_(tuple(EXCLUDED_ORDER_STATUSES)),
                )
                .group_by(Order.user_id)
            )
            order_stats = {
                user_id: (int(order_count or 0), money(total_spent or 0), last_order_date)
                for user_id, order_count, total_spent, last_order_date in order_stats_result.all()
            }
            for row in rows:
                order_count, total_spent, last_order_date = order_stats.get(
                    uuid.UUID(row["user_id"]),
                    (0, Decimal("0.00"), None),
                )
                row["order_count"] = order_count
                row["total_orders"] = order_count
                row["total_spent"] = format(total_spent, "f")
                row["last_order_date"] = last_order_date.isoformat() if last_order_date else None
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
        channels = body.get("channels")
        if channels is None:
            requested_channel = body.get("channel")
            channels = [requested_channel] if requested_channel else ["in_app", "push"]
        if not isinstance(channels, list):
            channels = [channels]
        allowed_channels = {"in_app", "push", "email", "whatsapp"}
        clean_channels = sorted({str(item).strip().lower() for item in channels if str(item).strip().lower() in allowed_channels})
        # Every new announcement is also visible inside the app and as a
        # device alert. Any selected external channel is kept alongside it.
        clean_channels = sorted({*clean_channels, "in_app", "push"})
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
                "channels": (row.extra_data or {}).get("channels") or ["in_app", "push"],
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
            channels = list(dict.fromkeys([
                *((row.extra_data or {}).get("channels") or []),
                "in_app",
                "push",
            ]))
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
                if settings.app_env != "test" and channel in {"email", "whatsapp"}:
                    status = "blocked_credentials"
                    blocked_credentials += 1
                else:
                    if channel in {"in_app", "push"}:
                        sent += 1
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
                                delivery_channels=("mobile_push",) if channel == "push" else ("in_app",),
                                deduplication_key=dedupe,
                            )
                        )
                    else:
                        sent += 1
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
                    message = {
                        "shipped": "تم شحن طلبك.",
                        "out_for_delivery": "طلبك في الطريق للتوصيل.",
                        "delivered": "تم تسليم طلبك.",
                        "delivery_failed": "تعذر إتمام التوصيل وسيتم التواصل معك.",
                    }.get(order_status, "تم تحديث حالة شحن طلبك.")
                    await NotificationService(session).create_notification(
                        NotificationPayload(
                            user_id=order.user_id,
                            title="تحديث شحن الطلب",
                            body=message,
                            notification_type="shipping_status_changed",
                            category="shipping",
                            priority="high",
                            action_url=f"/orders/{order.id}",
                            entity_type="order",
                            entity_id=str(order.id),
                            order_id=order.id,
                            payload={"deep_link": f"/orders/{order.id}", "order_status": order_status},
                            deduplication_key=f"courier-order-status:{assignment.id}:{status}",
                        )
                    )
        await session.commit()
        return serialize_record(assignment)


class ThemeAdminService:
    @staticmethod
    def require_access(roles: set[str]) -> None:
        _require_roles(roles, AUTHORIZED_THEME_ROLES, "theme_permission_denied")

    async def save(
        self,
        session: AsyncSession,
        *,
        actor: User,
        roles: set[str],
        body: dict[str, Any],
        setting_key: str = "default",
        publish: bool = True,
        commit: bool = True,
    ) -> dict[str, Any]:
        self.require_access(roles)
        if not isinstance(body, dict) or not _theme_payload_safe(body):
            raise HTTPException(status_code=422, detail="invalid_theme_payload")
        value = body.get("value", body)
        _validate_theme_setting_value(setting_key, value)
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
            "value": _jsonable(value),
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
        if commit:
            await session.commit()
        else:
            await session.flush()
        return serialize_record(row)

    async def preview(self, session: AsyncSession, *, actor: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        self.require_access(roles)
        if not isinstance(body, dict) or not _theme_payload_safe(body):
            raise HTTPException(status_code=422, detail="invalid_theme_payload")
        value = body.get("value", body)
        _validate_theme_settings_collection(value)
        token = uuid.uuid4().hex
        expires_at = _now() + timedelta(minutes=30)
        model = MODEL_BY_TABLE["theme_settings"]
        row = model(
            name=f"preview:{token}",
            status="preview",
            is_active=False,
            extra_data={"token": token, "value": _jsonable(value), "expires_at": expires_at.isoformat(), "created_by": str(actor.id)},
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
    def _partner_recipient_ids(row: Any) -> set[str]:
        extra = row.extra_data or {}
        values = extra.get("partner_ids") if isinstance(extra, dict) else None
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {str(value).strip() for value in values if str(value).strip()}

    @classmethod
    def _is_target_partner(cls, row: Any, user: User, roles: set[str]) -> bool:
        return "partner" in roles and str(user.id) in cls._partner_recipient_ids(row)

    @staticmethod
    def _can_view(row: Any, user: User, roles: set[str]) -> bool:
        return bool(
            roles.intersection(SUPPORT_STAFF_ROLES)
            or row.user_id == user.id
            or SupportWorkflowService._is_target_partner(row, user, roles)
        )

    async def list(self, session: AsyncSession, *, user: User, roles: set[str], limit: int = 500) -> list[dict[str, Any]]:
        model = MODEL_BY_TABLE["support_tickets"]
        statement = select(model).where(model.deleted_at.is_(None)).order_by(model.created_at.desc()).limit(min(limit, 500))
        if not roles.intersection(SUPPORT_STAFF_ROLES) and "partner" not in roles:
            statement = statement.where(model.user_id == user.id)
        return [
            serialize_record(row)
            for row in (await session.execute(statement)).scalars()
            if self._can_view(row, user, roles)
        ]

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
        await NotificationService(session).create_notification(NotificationPayload(
            user_id=ticket.user_id, title="تم فتح تذكرة الدعم",
            body="استلمنا تذكرتك وسيتابع فريق الدعم طلبك.", notification_type="ticket_opened",
            category="support", entity_type="support_ticket", entity_id=str(ticket.id),
            action_url="/support", deduplication_key=f"customer-ticket-opened:{ticket.id}",
            delivery_channels=("in_app", "mobile_push", "web_push"),
        ))
        await session.commit()
        return serialize_record(ticket)

    async def add_message(self, session: AsyncSession, *, ticket_id: uuid.UUID, user: User, roles: set[str], body: dict[str, Any]) -> dict[str, Any]:
        ticket = await self.get(session, ticket_id=ticket_id, user=user, roles=roles)
        message = str(body.get("message") or body.get("body") or "").strip()
        if len(message) < 2 or message.lower() in PLACEHOLDER_TEXT:
            raise HTTPException(status_code=422, detail="support_message_required")
        is_staff = bool(roles.intersection(SUPPORT_STAFF_ROLES))
        is_target_partner = self._is_target_partner(ticket, user, roles)
        is_responder = is_staff or is_target_partner
        model = MODEL_BY_TABLE["ticket_messages"]
        row = model(
            ticket_id=ticket.id,
            sender_id=user.id,
            message=message,
            is_staff=is_responder,
            extra_data={
                "customer_visible": not bool(body.get("internal")),
                "sender_role": "partner" if is_target_partner else "staff" if is_staff else "customer",
            },
        )
        session.add(row)
        extra = dict(ticket.extra_data or {})
        workflow = list(extra.get("workflow") or [])
        workflow.append({
            "status": "partner_reply" if is_target_partner else "staff_reply" if is_staff else "customer_reply",
            "at": _now().isoformat(),
            "by": str(user.id),
        })
        extra["workflow"] = workflow
        if is_responder:
            sla = dict(extra.get("sla") or {})
            sla.setdefault("first_response_at", _now().isoformat())
            extra["sla"] = sla
            await NotificationService(session).create_notification(
                NotificationPayload(
                    user_id=ticket.user_id,
                    title="رد من التاجر" if is_target_partner else "رد من الدعم",
                    body=message[:240],
                    notification_type="merchant_reply" if is_target_partner else "support_reply",
                    category="support",
                    priority="normal",
                    entity_type="support_ticket",
                    entity_id=str(ticket.id),
                    payload={"ticket_id": str(ticket.id), "sender": "partner" if is_target_partner else "support"},
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
