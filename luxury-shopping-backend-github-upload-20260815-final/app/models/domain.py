from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, UuidPrimaryKeyMixin


MONEY = Numeric(18, 2)


class User(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_salt: Mapped[str | None] = mapped_column(String(128))
    password_must_reset: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Profile(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    full_name: Mapped[str | None] = mapped_column(String(240))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    city: Mapped[str | None] = mapped_column(String(160))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(64))
    store_name: Mapped[str | None] = mapped_column(String(240))
    store_logo_url: Mapped[str | None] = mapped_column(Text)
    store_description: Mapped[str | None] = mapped_column(Text)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class UserRole(Base):
    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint(
            "role IN ('customer','admin','manager','finance','partner','courier','delivery','logistics','marketer','staff','employee')",
            name="ck_user_roles_known_role",
        ),
        Index("ix_user_roles_role", "role"),
    )


class StaffPermissionSet(Base, TimestampMixin):
    """Explicit per-user dashboard permissions.

    A missing row means that the user's role defaults are active.  An existing
    row is an explicit allow-list, so an empty list intentionally grants no
    dashboard actions to that staff member.
    """

    __tablename__ = "staff_permission_sets"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    permissions: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)


class RefreshToken(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "refresh_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))


class PasswordResetToken(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "password_reset_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64), index=True)


class AccountSecurity(Base, TimestampMixin):
    __tablename__ = "account_security"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    account_status: Mapped[str] = mapped_column(String(64), default="active", server_default="active", index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    security_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshTokenSecurity(Base, TimestampMixin):
    __tablename__ = "refresh_token_security"
    refresh_token_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    reused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetTokenState(Base, TimestampMixin):
    __tablename__ = "password_reset_token_state"
    reset_token_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class VerificationToken(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "verification_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    __table_args__ = (
        Index(
            "ix_verification_tokens_active_user_purpose",
            "user_id",
            "purpose",
            postgresql_where=text("used_at IS NULL AND invalidated_at IS NULL"),
        ),
    )


class PhoneOtpToken(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "phone_otp_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    otp_hash: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    requested_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    __table_args__ = (
        Index(
            "ix_phone_otp_active_user_phone_purpose",
            "user_id",
            "phone",
            "purpose",
            postgresql_where=text("used_at IS NULL AND invalidated_at IS NULL"),
        ),
    )


class LoginAttempt(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "login_attempts"
    email: Mapped[str] = mapped_column(String(320), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True)
    detail: Mapped[str | None] = mapped_column(Text)


class Category(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "categories"
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(String(240), index=True)
    slug: Mapped[str | None] = mapped_column(String(260), unique=True, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    image_url: Mapped[str | None] = mapped_column(Text)
    banner_url: Mapped[str | None] = mapped_column(Text)
    description_ar: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Brand(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "brands"
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(String(240), index=True)
    slug: Mapped[str | None] = mapped_column(String(260), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", index=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Product(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "products"
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(String(500), index=True)
    sku: Mapped[str | None] = mapped_column(String(160), unique=True, index=True)
    short_code: Mapped[str | None] = mapped_column(String(32), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    rich_description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    original_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency_code: Mapped[str] = mapped_column(String(8), server_default="YER")
    stock_quantity: Mapped[int] = mapped_column(Integer, server_default="0", index=True)
    min_stock_quantity: Mapped[int] = mapped_column(Integer, server_default="0")
    track_inventory: Mapped[bool] = mapped_column(Boolean, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True)
    approval_status: Mapped[str] = mapped_column(String(40), server_default="approved", index=True)
    approval_notes: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="SET NULL"), index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    partner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    image_url: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    tags: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    meta_title: Mapped[str | None] = mapped_column(String(500))
    meta_description: Mapped[str | None] = mapped_column(Text)
    promotional_title: Mapped[str | None] = mapped_column(String(500))
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_products_price_nonnegative"),
        CheckConstraint("stock_quantity >= 0", name="ck_products_stock_nonnegative"),
        Index("ix_products_catalog", "is_active", "approval_status", "category_id"),
    )


class ProductVariant(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "product_variants"
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    sku: Mapped[str | None] = mapped_column(String(160), unique=True, index=True)
    size: Mapped[str | None] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(120))
    color_hex: Mapped[str | None] = mapped_column(String(16))
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    original_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stock_quantity: Mapped[int] = mapped_column(Integer, server_default="0")
    image_url: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (CheckConstraint("stock_quantity >= 0", name="ck_variants_stock_nonnegative"),)


class FileAsset(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "file_assets"
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    policy_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    storage_provider: Mapped[str] = mapped_column(String(80), nullable=False, server_default="local_uploads")
    storage_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_filename: Mapped[str | None] = mapped_column(String(240))
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, server_default="available", index=True)
    scan_status: Mapped[str] = mapped_column(String(64), nullable=False, server_default="clean", index=True)
    scan_provider: Mapped[str | None] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(120), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_file_assets_size_positive"),
        CheckConstraint("visibility IN ('public','private')", name="ck_file_assets_visibility"),
        CheckConstraint(
            "scan_status IN ('clean','infected','blocked','not_required')",
            name="ck_file_assets_scan_status",
        ),
        Index("ix_file_assets_owner_policy", "owner_user_id", "policy_key"),
        Index(
            "ix_file_assets_active_storage_key",
            "storage_key",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class UserCart(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_cart"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_user_cart_quantity_positive"),
        UniqueConstraint("user_id", "product_id", "variant_id", name="uq_user_cart_line"),
        Index(
            "uq_user_cart_line_without_variant",
            "user_id",
            "product_id",
            unique=True,
            postgresql_where=text("variant_id IS NULL"),
        ),
        Index(
            "uq_user_cart_line_with_variant",
            "user_id",
            "product_id",
            "variant_id",
            unique=True,
            postgresql_where=text("variant_id IS NOT NULL"),
        ),
    )


class Wishlist(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "wishlist"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_wishlist_user_product"),)


class Order(Base, UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "orders"
    order_number: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(48), server_default="pending", index=True)
    total: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    subtotal: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    discount_total: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    shipping_total: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    currency_code: Mapped[str] = mapped_column(String(8), server_default="YER")
    payment_method: Mapped[str | None] = mapped_column(String(80))
    payment_status: Mapped[str] = mapped_column(String(48), server_default="pending", index=True)
    shipping_address: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_orders_total_nonnegative"),
        Index(
            "ix_orders_idempotency_user",
            "user_id",
            "idempotency_key",
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class OrderItem(Base, UuidPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "order_items"
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="SET NULL")
    )
    product_name: Mapped[str] = mapped_column(String(500), nullable=False)
    product_image: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    partner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0 AND total_price >= 0", name="ck_order_items_prices_nonnegative"),
    )


def _col(type_: Any, **kwargs: Any) -> Any:
    return mapped_column(type_, **kwargs)


COMMON_FIELD_SPECS: dict[str, Any] = {
    "user_id": lambda: _col(UUID(as_uuid=True), index=True),
    "order_id": lambda: _col(UUID(as_uuid=True), index=True),
    "product_id": lambda: _col(UUID(as_uuid=True), index=True),
    "variant_id": lambda: _col(UUID(as_uuid=True), index=True),
    "partner_id": lambda: _col(UUID(as_uuid=True), index=True),
    "courier_id": lambda: _col(UUID(as_uuid=True), index=True),
    "assignment_id": lambda: _col(UUID(as_uuid=True), index=True),
    "ticket_id": lambda: _col(UUID(as_uuid=True), index=True),
    "sender_id": lambda: _col(UUID(as_uuid=True), index=True),
    "recipient_id": lambda: _col(UUID(as_uuid=True), index=True),
    "created_by": lambda: _col(UUID(as_uuid=True), index=True),
    "notification_id": lambda: _col(UUID(as_uuid=True), index=True),
    "event_id": lambda: _col(UUID(as_uuid=True), index=True),
    "aggregate_id": lambda: _col(UUID(as_uuid=True), index=True),
    "reviewed_by": lambda: _col(UUID(as_uuid=True), index=True),
    "name": lambda: _col(String(500), index=True),
    "name_en": lambda: _col(String(500)),
    "title": lambda: _col(String(500), index=True),
    "label": lambda: _col(String(240), index=True),
    "slug": lambda: _col(String(500), index=True),
    "email": lambda: _col(String(320), index=True),
    "phone": lambda: _col(String(32), index=True),
    "recipient_name": lambda: _col(String(240), index=True),
    "governorate": lambda: _col(String(160), index=True),
    "city": lambda: _col(String(160)),
    "address": lambda: _col(Text),
    "status": lambda: _col(String(64), index=True),
    "type": lambda: _col(String(64), index=True),
    "notification_type": lambda: _col(String(80), index=True),
    "category": lambda: _col(String(80), index=True),
    "priority": lambda: _col(String(32), index=True),
    "platform": lambda: _col(String(32), index=True),
    "device_id": lambda: _col(String(160), index=True),
    "device_name": lambda: _col(String(240)),
    "app_version": lambda: _col(String(80)),
    "environment": lambda: _col(String(80), index=True),
    "endpoint": lambda: _col(Text),
    "browser": lambda: _col(String(120)),
    "provider": lambda: _col(String(80), index=True),
    "response_code": lambda: _col(String(80)),
    "error_code": lambda: _col(String(160)),
    "event_type": lambda: _col(String(120), index=True),
    "aggregate_type": lambda: _col(String(120), index=True),
    "entity_type": lambda: _col(String(120), index=True),
    "entity_id": lambda: _col(String(160), index=True),
    "action_type": lambda: _col(String(80)),
    "deduplication_key": lambda: _col(String(240), index=True),
    "code": lambda: _col(String(160), index=True),
    "subject": lambda: _col(String(500)),
    "message": lambda: _col(Text),
    "body": lambda: _col(Text),
    "description": lambda: _col(Text),
    "notes": lambda: _col(Text),
    "reason": lambda: _col(Text),
    "target": lambda: _col(String(500)),
    "channel": lambda: _col(String(80), index=True),
    "token": lambda: _col(Text),
    "p256dh": lambda: _col(Text),
    "auth": lambda: _col(Text),
    "user_agent": lambda: _col(Text),
    "image_url": lambda: _col(Text),
    "logo_url": lambda: _col(Text),
    "url": lambda: _col(Text),
    "path": lambda: _col(Text),
    "is_active": lambda: _col(Boolean, server_default="true", index=True),
    "is_read": lambda: _col(Boolean, server_default="false", index=True),
    "in_app_enabled": lambda: _col(Boolean, server_default="true"),
    "mobile_push_enabled": lambda: _col(Boolean, server_default="true"),
    "web_push_enabled": lambda: _col(Boolean, server_default="true"),
    "order_updates": lambda: _col(Boolean, server_default="true"),
    "payment_updates": lambda: _col(Boolean, server_default="true"),
    "shipping_updates": lambda: _col(Boolean, server_default="true"),
    "promotional_notifications": lambda: _col(Boolean, server_default="true"),
    "support_updates": lambda: _col(Boolean, server_default="true"),
    "security_notifications": lambda: _col(Boolean, server_default="true"),
    "system_notifications": lambda: _col(Boolean, server_default="true"),
    "is_staff": lambda: _col(Boolean, server_default="false"),
    "is_default": lambda: _col(Boolean, server_default="false"),
    "is_terminal": lambda: _col(Boolean, server_default="false"),
    "notify_customer": lambda: _col(Boolean, server_default="false"),
    "is_acknowledged": lambda: _col(Boolean, server_default="false", index=True),
    "amount": lambda: _col(MONEY, server_default="0"),
    "value": lambda: _col(MONEY, server_default="0"),
    "total": lambda: _col(MONEY, server_default="0"),
    "fee": lambda: _col(MONEY, server_default="0"),
    "balance": lambda: _col(MONEY, server_default="0"),
    "sort_order": lambda: _col(Integer, server_default="0"),
    "quantity": lambda: _col(Integer, server_default="0"),
    "attempts": lambda: _col(Integer, server_default="0"),
    "failure_count": lambda: _col(Integer, server_default="0"),
    "attempt_number": lambda: _col(Integer, server_default="1"),
    "latitude": lambda: _col(Numeric(10, 7)),
    "longitude": lambda: _col(Numeric(10, 7)),
    "expires_at": lambda: _col(DateTime(timezone=True), index=True),
    "read_at": lambda: _col(DateTime(timezone=True)),
    "reviewed_at": lambda: _col(DateTime(timezone=True)),
    "last_seen_at": lambda: _col(DateTime(timezone=True)),
    "invalidated_at": lambda: _col(DateTime(timezone=True)),
    "available_at": lambda: _col(DateTime(timezone=True), index=True),
    "processed_at": lambda: _col(DateTime(timezone=True)),
    "last_success_at": lambda: _col(DateTime(timezone=True)),
    "last_failure_at": lambda: _col(DateTime(timezone=True)),
    "sent_at": lambda: _col(DateTime(timezone=True)),
    "delivered_at": lambda: _col(DateTime(timezone=True)),
    "failed_at": lambda: _col(DateTime(timezone=True)),
    "last_error": lambda: _col(Text),
    "payload": lambda: _col(JSONB, default=dict, server_default="{}"),
}


RESOURCE_SPECS: dict[str, tuple[str, ...]] = {
    "push_tokens": ("user_id", "token", "platform", "device_id", "app_version", "device_name", "environment", "status", "is_active", "last_seen_at", "invalidated_at", "failure_count"),
    "audit_logs": ("user_id", "type", "description"),
    "data_access_logs": ("user_id", "type", "description"),
    "security_events": ("user_id", "type", "status", "description", "path"),
    "account_deletion_requests": ("user_id", "status", "reason"),
    "banners": ("title", "image_url", "url", "status", "is_active", "sort_order"),
    "banner_history": ("title", "image_url", "status", "created_by"),
    "product_reviews": ("user_id", "product_id", "status", "title", "body"),
    "product_likes": ("user_id", "product_id"),
    "product_comparisons": ("user_id", "product_id"),
    "customer_addresses": (
        "user_id", "label", "recipient_name", "phone", "governorate",
        "city", "address", "latitude", "longitude", "is_default",
    ),
    "store_reviews": ("user_id", "partner_id", "status", "title", "body"),
    "local_merchants": ("user_id", "name", "name_en", "email", "phone", "status", "description", "logo_url", "is_active"),
    "partner_storefronts": ("user_id", "partner_id", "name", "email", "phone", "status", "description", "logo_url", "is_active"),
    "order_payments": ("order_id", "status", "type", "amount"),
    "order_status_history": ("order_id", "status", "notes"),
    "order_fulfillments": ("order_id", "partner_id", "status", "notes"),
    "order_shipping": ("order_id", "status", "fee", "description"),
    "shipping_history": ("order_id", "status", "notes"),
    "refunds": ("order_id", "user_id", "status", "amount", "reason"),
    "payments": ("order_id", "user_id", "status", "type", "amount"),
    "payment_receipts": ("order_id", "user_id", "status", "image_url", "amount", "reviewed_by", "reviewed_at"),
    "shipping_zones": ("name", "status", "fee", "is_active", "sort_order"),
    "shipping_carriers": ("name", "name_en", "code", "status", "logo_url", "fee", "is_active", "is_default", "sort_order"),
    "shipping_stages": ("name", "name_en", "code", "status", "is_terminal", "notify_customer", "sort_order"),
    "couriers": ("user_id", "name", "phone", "status"),
    "courier_assignments": ("courier_id", "user_id", "order_id", "status"),
    "courier_location_updates": ("courier_id", "user_id", "assignment_id", "latitude", "longitude"),
    "coupons": ("code", "title", "status", "amount", "is_active", "expires_at"),
    "coupon_usage": ("user_id", "order_id", "amount"),
    "loyalty_settings": ("name", "status", "is_active"),
    "loyalty_tiers": ("name", "name_en", "status", "amount", "sort_order", "is_active"),
    "user_loyalty": ("user_id", "status", "balance"),
    "points_transactions": ("user_id", "order_id", "type", "amount", "description"),
    "partner_applications": ("user_id", "name", "email", "phone", "status", "description", "logo_url", "reviewed_by", "reviewed_at"),
    "partner_profiles": ("user_id", "partner_id", "name", "email", "phone", "status", "logo_url"),
    "partner_wallets": ("partner_id", "status", "balance"),
    "partner_contracts": ("partner_id", "status", "is_active"),
    "partner_coupons": ("partner_id", "code", "status", "amount", "is_active", "expires_at"),
    "partner_notification_preferences": ("partner_id", "status", "is_active"),
    "partner_order_items": ("partner_id", "order_id", "product_id", "quantity", "status", "amount"),
    "partner_order_requests": ("partner_id", "order_id", "status", "notes"),
    "partner_payments": ("partner_id", "order_id", "status", "amount"),
    "local_shopping_requests": ("user_id", "status", "description", "amount"),
    "international_orders": ("user_id", "status", "description", "amount"),
    "global_sites": ("name", "name_en", "url", "logo_url", "status", "is_active", "sort_order"),
    "currencies": ("name", "name_en", "code", "status", "is_active"),
    "suppliers": ("name", "name_en", "email", "phone", "status", "description", "is_active"),
    "warehouses": ("name", "status", "description", "is_active"),
    "inventory": ("product_id", "variant_id", "quantity", "status"),
    "inventory_movements": ("product_id", "variant_id", "quantity", "type", "status", "notes"),
    "employee_payments": ("user_id", "status", "amount", "notes"),
    "general_expenses": ("status", "type", "amount", "description"),
    "marketers": ("user_id", "name", "phone", "status"),
    "marketer_commissions": ("user_id", "order_id", "status", "amount"),
    "marketer_payments": ("user_id", "status", "amount", "notes"),
    "sales_forecasts": ("status", "type", "amount", "description"),
    "analytics_events": ("user_id", "type", "description"),
    "public_marketer_codes": ("user_id", "code", "status", "is_active"),
    "site_settings": ("name", "status", "is_active"),
    "theme_settings": ("name", "status", "is_active"),
    "site_content": ("name", "title", "status", "body"),
    "site_menus": ("name", "title", "url", "status", "is_active", "sort_order"),
    "social_links": ("name", "url", "status", "is_active", "sort_order"),
    "static_pages": ("title", "slug", "status", "body", "is_active"),
    "page_sections": ("title", "status", "body", "sort_order", "is_active"),
    "page_versions": ("title", "status", "body", "created_by"),
    "blog_articles": ("title", "slug", "status", "body", "image_url", "is_active"),
    "custom_elements": ("name", "type", "status", "body", "is_active", "sort_order"),
    "form_settings": ("name", "type", "status", "is_active"),
    "notifications": ("user_id", "recipient_id", "order_id", "title", "body", "message", "type", "notification_type", "category", "priority", "image_url", "action_type", "url", "entity_type", "entity_id", "payload", "status", "is_read", "read_at", "expires_at", "created_by", "source", "deduplication_key"),
    "admin_notifications": ("user_id", "recipient_id", "title", "body", "message", "type", "notification_type", "category", "priority", "image_url", "action_type", "url", "entity_type", "entity_id", "payload", "status", "is_read", "read_at", "expires_at", "created_by", "source", "deduplication_key"),
    "support_tickets": ("user_id", "subject", "status", "description"),
    "ticket_messages": ("ticket_id", "sender_id", "message", "is_staff"),
    "marketing_campaigns": ("title", "status", "message", "created_by"),
    "notification_preferences": ("user_id", "in_app_enabled", "mobile_push_enabled", "web_push_enabled", "order_updates", "payment_updates", "shipping_updates", "promotional_notifications", "support_updates", "security_notifications", "system_notifications", "status"),
    "web_push_subscriptions": ("user_id", "endpoint", "p256dh", "auth", "browser", "user_agent", "status", "is_active", "last_success_at", "last_failure_at", "failure_count"),
    "notification_outbox": ("event_id", "event_type", "aggregate_type", "aggregate_id", "user_id", "payload", "status", "attempts", "available_at", "processed_at", "last_error", "type", "title", "message"),
    "notification_delivery_attempts": ("notification_id", "user_id", "channel", "target", "provider", "status", "response_code", "error_code", "attempt_number", "sent_at", "delivered_at", "failed_at"),
    "email_outbox": ("user_id", "title", "status", "email", "message"),
    "whatsapp_outbox": ("user_id", "title", "status", "phone", "message"),
    "sync_events": ("user_id", "type", "status", "description"),
    "client_mutations": ("user_id", "type", "status", "description"),
    "sync_dead_letters": ("user_id", "type", "status", "description"),
    "cache_snapshots": ("user_id", "type", "status", "path"),
    "backup_records": ("user_id", "status", "path", "description"),
    "deployment_checks": ("status", "type", "description"),
    "report_exports": ("user_id", "type", "status", "path", "description"),
    "contact_messages": ("user_id", "name", "email", "phone", "subject", "message", "status"),
    "color_options": ("name", "code", "status", "is_active", "sort_order"),
    "size_options": ("name", "code", "status", "is_active", "sort_order"),
    "financial_vouchers": ("user_id", "type", "status", "amount", "description"),
    "cash_transactions": ("user_id", "type", "status", "amount", "description"),
    "financial_reports": ("type", "status", "amount", "description"),
    "operational_days": ("user_id", "status", "description"),
    "operational_alerts": ("user_id", "type", "status", "description"),
    "risk_alerts": ("user_id", "type", "status", "description", "is_acknowledged"),
    "kpi_metrics": ("name", "type", "status", "value", "description", "sort_order"),
    "purchase_items": ("order_id", "product_id", "quantity", "amount", "status"),
    "international_purchases": ("user_id", "status", "amount", "description"),
    "international_order_payments": ("order_id", "status", "amount"),
    "partner_settlements": ("partner_id", "status", "amount"),
    "order_financials": ("order_id", "status", "amount"),
    "vouchers": ("user_id", "type", "status", "amount", "description"),
    "inventory_locations": ("name", "status", "description", "is_active"),
}


def _resource_class_name(table: str) -> str:
    return "".join(part.capitalize() for part in table.split("_")) + "Record"


def _build_resource_model(table: str, fields: tuple[str, ...]) -> type[Base]:
    attrs: dict[str, Any] = {
        "__tablename__": table,
        "__module__": __name__,
        "__allow_unmapped__": True,
        "id": mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        "created_at": mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "updated_at": mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
        "deleted_at": mapped_column(DateTime(timezone=True), nullable=True),
    }
    for field in fields:
        if table == "customer_addresses" and field == "user_id":
            attrs[field] = mapped_column(
                UUID(as_uuid=True),
                ForeignKey("users.id", ondelete="CASCADE"),
                index=True,
            )
            continue
        factory = COMMON_FIELD_SPECS.get(field)
        attrs[field] = factory() if factory else mapped_column(JSONB)
    attrs["extra_data"] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    if table == "customer_addresses":
        attrs["__table_args__"] = (
            Index(
                "uq_customer_addresses_one_default_per_user",
                "user_id",
                unique=True,
                postgresql_where=text("is_default IS TRUE AND deleted_at IS NULL"),
            ),
        )
    return type(_resource_class_name(table), (Base,), attrs)


MODEL_BY_TABLE: dict[str, type[Base]] = {
    "users": User,
    "profiles": Profile,
    "user_roles": UserRole,
    "staff_permission_sets": StaffPermissionSet,
    "refresh_tokens": RefreshToken,
    "password_reset_tokens": PasswordResetToken,
    "account_security": AccountSecurity,
    "refresh_token_security": RefreshTokenSecurity,
    "password_reset_token_state": PasswordResetTokenState,
    "verification_tokens": VerificationToken,
    "phone_otp_tokens": PhoneOtpToken,
    "login_attempts": LoginAttempt,
    "categories": Category,
    "brands": Brand,
    "products": Product,
    "product_variants": ProductVariant,
    "file_assets": FileAsset,
    "user_cart": UserCart,
    "wishlist": Wishlist,
    "orders": Order,
    "order_items": OrderItem,
}

for _table, _fields in RESOURCE_SPECS.items():
    if _table not in MODEL_BY_TABLE:
        MODEL_BY_TABLE[_table] = _build_resource_model(_table, _fields)

RESOURCE_TABLES = frozenset(MODEL_BY_TABLE)
