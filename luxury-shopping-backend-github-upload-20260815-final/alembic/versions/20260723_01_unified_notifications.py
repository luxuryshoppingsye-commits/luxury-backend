from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260723_01"
down_revision = "20260720_05"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_column(table: str, name: str, column: sa.Column) -> None:
    if name not in _columns(table):
        op.add_column(table, column)


def _create_index(name: str, table: str, columns: list[str], *, unique: bool = False, where: str | None = None) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique, postgresql_where=sa.text(where) if where else None)


def _resource_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("extra_data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    ]


def upgrade() -> None:
    existing = _tables()
    if "notification_preferences" not in existing:
        op.create_table(
            "notification_preferences",
            *_resource_columns(),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("in_app_enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("mobile_push_enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("web_push_enabled", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("order_updates", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("payment_updates", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("shipping_updates", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("promotional_notifications", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("support_updates", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("security_notifications", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("system_notifications", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("status", sa.String(64), server_default="active"),
        )
    if "web_push_subscriptions" not in existing:
        op.create_table(
            "web_push_subscriptions",
            *_resource_columns(),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column("p256dh", sa.Text(), nullable=False),
            sa.Column("auth", sa.Text(), nullable=False),
            sa.Column("browser", sa.String(120)),
            sa.Column("user_agent", sa.Text()),
            sa.Column("status", sa.String(64), server_default="active"),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_failure_at", sa.DateTime(timezone=True)),
            sa.Column("failure_count", sa.Integer(), server_default="0"),
        )
    if "notification_delivery_attempts" not in existing:
        op.create_table(
            "notification_delivery_attempts",
            *_resource_columns(),
            sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("channel", sa.String(80), nullable=False),
            sa.Column("target", sa.Text()),
            sa.Column("provider", sa.String(80)),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("response_code", sa.String(80)),
            sa.Column("error_code", sa.String(160)),
            sa.Column("attempt_number", sa.Integer(), server_default="1"),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("failed_at", sa.DateTime(timezone=True)),
        )

    for table in ("notifications", "admin_notifications"):
        if table in _tables():
            _add_column(table, "notification_type", sa.Column("notification_type", sa.String(80)))
            _add_column(table, "category", sa.Column("category", sa.String(80)))
            _add_column(table, "priority", sa.Column("priority", sa.String(32)))
            _add_column(table, "image_url", sa.Column("image_url", sa.Text()))
            _add_column(table, "action_type", sa.Column("action_type", sa.String(80)))
            _add_column(table, "url", sa.Column("url", sa.Text()))
            _add_column(table, "entity_type", sa.Column("entity_type", sa.String(120)))
            _add_column(table, "entity_id", sa.Column("entity_id", sa.String(160)))
            _add_column(table, "payload", sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}"))
            _add_column(table, "expires_at", sa.Column("expires_at", sa.DateTime(timezone=True)))
            _add_column(table, "created_by", sa.Column("created_by", postgresql.UUID(as_uuid=True)))
            _add_column(table, "source", sa.Column("source", sa.String(80)))
            _add_column(table, "deduplication_key", sa.Column("deduplication_key", sa.String(240)))
            _create_index(f"ix_{table}_unread_user", table, ["user_id", "is_read"])
            _create_index(f"ux_{table}_dedup_active", table, ["deduplication_key"], unique=True, where="deduplication_key IS NOT NULL AND deleted_at IS NULL")

    if "push_tokens" in _tables():
        _add_column("push_tokens", "token", sa.Column("token", sa.Text()))
        _add_column("push_tokens", "platform", sa.Column("platform", sa.String(32)))
        _add_column("push_tokens", "device_id", sa.Column("device_id", sa.String(160)))
        _add_column("push_tokens", "app_version", sa.Column("app_version", sa.String(80)))
        _add_column("push_tokens", "device_name", sa.Column("device_name", sa.String(240)))
        _add_column("push_tokens", "environment", sa.Column("environment", sa.String(80)))
        _add_column("push_tokens", "is_active", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")))
        _add_column("push_tokens", "last_seen_at", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
        _add_column("push_tokens", "invalidated_at", sa.Column("invalidated_at", sa.DateTime(timezone=True)))
        _add_column("push_tokens", "failure_count", sa.Column("failure_count", sa.Integer(), server_default="0"))
        _create_index("ux_push_tokens_token_active", "push_tokens", ["token"], unique=True, where="token IS NOT NULL AND is_active IS TRUE")

    if "notification_outbox" in _tables():
        _add_column("notification_outbox", "event_id", sa.Column("event_id", postgresql.UUID(as_uuid=True)))
        _add_column("notification_outbox", "event_type", sa.Column("event_type", sa.String(120)))
        _add_column("notification_outbox", "aggregate_type", sa.Column("aggregate_type", sa.String(120)))
        _add_column("notification_outbox", "aggregate_id", sa.Column("aggregate_id", postgresql.UUID(as_uuid=True)))
        _add_column("notification_outbox", "payload", sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}"))
        _add_column("notification_outbox", "attempts", sa.Column("attempts", sa.Integer(), server_default="0"))
        _add_column("notification_outbox", "available_at", sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column("notification_outbox", "processed_at", sa.Column("processed_at", sa.DateTime(timezone=True)))
        _add_column("notification_outbox", "last_error", sa.Column("last_error", sa.Text()))
        _create_index("ix_notification_outbox_pending", "notification_outbox", ["status", "available_at"])

    _create_index("ux_notification_preferences_user", "notification_preferences", ["user_id"], unique=True, where="deleted_at IS NULL")
    _create_index("ux_web_push_endpoint_active", "web_push_subscriptions", ["endpoint"], unique=True, where="is_active IS TRUE")


def downgrade() -> None:
    pass
