from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MODEL_BY_TABLE


ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trial"})
BLOCKED_SUBSCRIPTION_STATUSES = frozenset(
    {"pending_payment", "past_due", "suspended", "expired", "rejected", "cancelled"}
)
ACTIVE_CONTRACT_STATUSES = frozenset({"active", "accepted", "approved", "trial"})


@dataclass(frozen=True)
class PartnerSubscriptionState:
    status: str
    is_active: bool
    reason: str | None
    plan: str
    amount: Decimal
    currency_code: str
    period_days: int
    started_at: str | None
    expires_at: str | None
    pending_payment_id: str | None = None


def _text(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _amount(value: Any, fallback: Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return fallback
    return amount if amount > 0 else fallback


def _period_days(value: Any, fallback: int) -> int:
    try:
        period = int(value)
    except (TypeError, ValueError):
        period = fallback
    return max(1, min(period, 366))


def _baseline_active(contract_status: Any, contract_is_active: Any) -> bool:
    return contract_is_active is not False and _text(contract_status).lower() in ACTIVE_CONTRACT_STATUSES


def subscription_state(
    extra_data: Any,
    *,
    contract_status: Any = "active",
    contract_is_active: Any = True,
    now: datetime | None = None,
    pending_payment_id: str | None = None,
) -> PartnerSubscriptionState:
    extra = extra_data if isinstance(extra_data, dict) else {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    baseline_active = _baseline_active(contract_status, contract_is_active)
    raw_status = _text(extra.get("subscription_status")).lower()
    # A contract created before the subscription workflow has no payment
    # evidence. Treat it as awaiting payment so legacy merchants cannot stay
    # publicly visible without an approved subscription.
    status = raw_status or "pending_payment"
    expires_at = _parse_datetime(extra.get("subscription_expires_at"))
    started_at = _parse_datetime(extra.get("subscription_started_at"))
    amount = _amount(extra.get("subscription_amount"), get_settings().partner_subscription_amount_yer)
    period_days = _period_days(
        extra.get("subscription_period_days"),
        get_settings().partner_subscription_period_days,
    )
    is_active = baseline_active and status in ACTIVE_SUBSCRIPTION_STATUSES
    reason: str | None = None
    if not baseline_active:
        reason = "contract_inactive"
    elif status in BLOCKED_SUBSCRIPTION_STATUSES:
        reason = status
    elif expires_at is not None and expires_at <= now:
        status = "expired"
        is_active = False
        reason = "expired"
    elif not is_active:
        reason = "subscription_inactive"
    return PartnerSubscriptionState(
        status=status,
        is_active=is_active,
        reason=reason,
        plan=_text(extra.get("subscription_plan"), default="monthly"),
        amount=amount.quantize(Decimal("0.01")),
        currency_code=_text(extra.get("subscription_currency"), default="YER").upper(),
        period_days=period_days,
        started_at=started_at.isoformat() if started_at else None,
        expires_at=expires_at.isoformat() if expires_at else None,
        pending_payment_id=pending_payment_id,
    )


def subscription_payload(
    contract: Any | None,
    *,
    pending_payment_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = subscription_state(
        getattr(contract, "extra_data", {}) if contract is not None else {},
        contract_status=getattr(contract, "status", "pending") if contract is not None else "pending",
        contract_is_active=getattr(contract, "is_active", False) if contract is not None else False,
        now=now,
        pending_payment_id=pending_payment_id,
    )
    return {
        "status": state.status,
        "is_active": state.is_active,
        "isActive": state.is_active,
        "reason": state.reason,
        "plan": state.plan,
        "amount": str(state.amount),
        "currency_code": state.currency_code,
        "currencyCode": state.currency_code,
        "period_days": state.period_days,
        "periodDays": state.period_days,
        "started_at": state.started_at,
        "startedAt": state.started_at,
        "expires_at": state.expires_at,
        "expiresAt": state.expires_at,
        "pending_payment_id": state.pending_payment_id,
        "pendingPaymentId": state.pending_payment_id,
    }


def _contract_subscription_clause(contract_model: type[Any], *, now: datetime | None = None) -> Any:
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    contract_status = func.lower(func.trim(func.coalesce(contract_model.status, "")))
    subscription_status = func.lower(
        func.trim(func.coalesce(contract_model.extra_data["subscription_status"].astext, ""))
    )
    expires_at = func.trim(func.coalesce(contract_model.extra_data["subscription_expires_at"].astext, ""))
    expiry_ok = or_(expires_at == "", expires_at > now_iso)
    return and_(
        contract_model.deleted_at.is_(None),
        contract_model.is_active.is_(True),
        contract_status.in_(tuple(ACTIVE_CONTRACT_STATUSES)),
        and_(subscription_status.in_(tuple(ACTIVE_SUBSCRIPTION_STATUSES)), expiry_ok),
    )


def partner_subscription_exists_clause(partner_column: Any, *, now: datetime | None = None) -> Any:
    contract_model = MODEL_BY_TABLE.get("partner_contracts")
    if contract_model is None:
        return False
    return exists(
        select(contract_model.id).where(
            contract_model.partner_id == partner_column,
            _contract_subscription_clause(contract_model, now=now),
        )
    )


def public_partner_product_clause(product_model: type[Any], *, now: datetime | None = None) -> Any:
    storefront_model = MODEL_BY_TABLE.get("partner_storefronts")
    if storefront_model is None or not hasattr(product_model, "partner_id"):
        return True
    storefront_exists = exists(
        select(storefront_model.id).where(
            or_(storefront_model.partner_id == product_model.partner_id, storefront_model.user_id == product_model.partner_id),
            storefront_model.deleted_at.is_(None),
            storefront_model.is_active.is_(True),
        )
    )
    return or_(
        product_model.partner_id.is_(None),
        and_(storefront_exists, partner_subscription_exists_clause(product_model.partner_id, now=now)),
    )


def public_partner_storefront_clause(storefront_model: type[Any], *, now: datetime | None = None) -> Any:
    partner_columns = []
    if hasattr(storefront_model, "partner_id"):
        partner_columns.append(storefront_model.partner_id)
    if hasattr(storefront_model, "user_id"):
        partner_columns.append(storefront_model.user_id)
    partner_clause = or_(*[
        partner_subscription_exists_clause(column, now=now)
        for column in partner_columns
    ]) if partner_columns else False
    return and_(
        storefront_model.deleted_at.is_(None),
        storefront_model.is_active.is_(True),
        partner_clause,
    )


async def active_partner_ids(
    session: AsyncSession,
    partner_ids: Iterable[Any],
    *,
    now: datetime | None = None,
) -> set[Any]:
    normalized = {value for value in partner_ids if value}
    if not normalized:
        return set()
    model = MODEL_BY_TABLE.get("partner_contracts")
    if model is None:
        return set()
    result = await session.execute(
        select(model)
        .where(model.partner_id.in_(normalized), model.deleted_at.is_(None))
        .order_by(model.updated_at.desc())
    )
    latest: dict[Any, Any] = {}
    for row in result.scalars():
        latest.setdefault(row.partner_id, row)
    return {
        partner_id
        for partner_id, row in latest.items()
        if subscription_state(
            getattr(row, "extra_data", {}),
            contract_status=getattr(row, "status", None),
            contract_is_active=getattr(row, "is_active", None),
            now=now,
        ).is_active
    }


async def activate_subscription_from_payment(
    session: AsyncSession,
    *,
    partner_id: Any,
    payment_id: Any,
    amount: Any,
    period_days: Any = None,
    actor_id: Any,
) -> dict[str, Any]:
    model = MODEL_BY_TABLE["partner_contracts"]
    contract = (
        await session.execute(
            select(model)
            .where(model.partner_id == partner_id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if contract is None:
        contract = model(partner_id=partner_id, status="active", is_active=True, extra_data={})
        session.add(contract)
        await session.flush()
    extra = dict(getattr(contract, "extra_data", {}) or {})
    current_expiry = _parse_datetime(extra.get("subscription_expires_at"))
    period = _period_days(
        period_days or extra.get("subscription_period_days"),
        get_settings().partner_subscription_period_days,
    )
    start = current_expiry if current_expiry and current_expiry > now else now
    expiry = start + timedelta(days=period)
    extra.update(
        {
            "subscription_status": "active",
            "subscription_plan": _text(extra.get("subscription_plan"), default="monthly"),
            "subscription_amount": str(_amount(amount, get_settings().partner_subscription_amount_yer)),
            "subscription_currency": "YER",
            "subscription_period_days": period,
            "subscription_started_at": extra.get("subscription_started_at") or now.isoformat(),
            "subscription_expires_at": expiry.isoformat(),
            "subscription_paid_at": now.isoformat(),
            "subscription_payment_id": str(payment_id),
            "subscription_activated_by": str(actor_id),
        }
    )
    contract.status = "active"
    contract.is_active = True
    contract.extra_data = extra
    storefront_model = MODEL_BY_TABLE.get("partner_storefronts")
    if storefront_model is not None:
        storefronts = (
            await session.execute(
                select(storefront_model)
                .where(
                    or_(storefront_model.partner_id == partner_id, storefront_model.user_id == partner_id),
                    storefront_model.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
        for storefront in storefronts:
            storefront.status = "active"
            storefront.is_active = True
    await session.flush()
    return subscription_payload(contract, now=now)


async def update_partner_subscription(
    session: AsyncSession,
    *,
    partner_id: Any,
    status: str,
    actor_id: Any,
    expires_at: Any = None,
    period_days: Any = None,
    amount: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    normalized = _text(status).lower()
    if normalized not in ACTIVE_SUBSCRIPTION_STATUSES | BLOCKED_SUBSCRIPTION_STATUSES:
        raise ValueError("invalid_subscription_status")
    model = MODEL_BY_TABLE["partner_contracts"]
    contract = (
        await session.execute(
            select(model)
            .where(model.partner_id == partner_id, model.deleted_at.is_(None))
            .order_by(model.updated_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contract is None:
        contract = model(partner_id=partner_id, status="active", is_active=True, extra_data={})
        session.add(contract)
        await session.flush()
    extra = dict(getattr(contract, "extra_data", {}) or {})
    extra["subscription_status"] = normalized
    extra["subscription_currency"] = "YER"
    extra["subscription_amount"] = str(_amount(amount or extra.get("subscription_amount"), get_settings().partner_subscription_amount_yer))
    normalized_period_days = _period_days(
        period_days or extra.get("subscription_period_days"),
        get_settings().partner_subscription_period_days,
    )
    extra["subscription_period_days"] = normalized_period_days
    if expires_at is not None:
        parsed_expiry = _parse_datetime(expires_at)
        if parsed_expiry is None:
            raise ValueError("invalid_subscription_expiry")
        extra["subscription_expires_at"] = parsed_expiry.isoformat()
    elif normalized in ACTIVE_SUBSCRIPTION_STATUSES and (
        _parse_datetime(extra.get("subscription_expires_at")) is None
        or _parse_datetime(extra.get("subscription_expires_at")) <= datetime.now(timezone.utc)
    ):
        extra["subscription_started_at"] = datetime.now(timezone.utc).isoformat()
        extra["subscription_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(days=normalized_period_days)
        ).isoformat()
    if reason:
        extra["subscription_admin_reason"] = _text(reason, default="")[:500]
    extra["subscription_updated_at"] = datetime.now(timezone.utc).isoformat()
    extra["subscription_updated_by"] = str(actor_id)
    contract.status = "active" if normalized in ACTIVE_SUBSCRIPTION_STATUSES else normalized
    contract.is_active = normalized in ACTIVE_SUBSCRIPTION_STATUSES
    contract.extra_data = extra
    storefront_model = MODEL_BY_TABLE.get("partner_storefronts")
    if storefront_model is not None:
        storefronts = (
            await session.execute(
                select(storefront_model)
                .where(
                    or_(storefront_model.partner_id == partner_id, storefront_model.user_id == partner_id),
                    storefront_model.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
        for storefront in storefronts:
            storefront.is_active = normalized in ACTIVE_SUBSCRIPTION_STATUSES
            storefront.status = "active" if storefront.is_active else normalized
    await session.flush()
    return subscription_payload(contract)
