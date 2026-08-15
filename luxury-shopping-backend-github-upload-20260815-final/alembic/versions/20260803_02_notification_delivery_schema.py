"""Create notification tables that may be absent from older production databases."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260803_02"
down_revision = "20260803_01"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _resource_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("extra_data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    ]


def _add_column(table: str, name: str, column: sa.Column) -> None:
    if name not in _columns(table):
        op.add_column(table, column)


def _create_index(name: str, table: str, columns: list[str], *, unique: bool = False, where: str | None = None) -> None:
    if name not in _indexes(table):
        op.create_index(
            name,
            table,
            columns,
            unique=unique,
            postgresql_where=sa.text(where) if where else None,
        )


def upgrade() -> None:
    tables = _tables()

    if "notification_preferences" not in tables:
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
    else:
        for name, column in (
            ("user_id", sa.Column("user_id", postgresql.UUID(as_uuid=True))),
            ("in_app_enabled", sa.Column("in_app_enabled", sa.Boolean(), server_default=sa.text("true"))),
            ("mobile_push_enabled", sa.Column("mobile_push_enabled", sa.Boolean(), server_default=sa.text("true"))),
            ("web_push_enabled", sa.Column("web_push_enabled", sa.Boolean(), server_default=sa.text("true"))),
            ("order_updates", sa.Column("order_updates", sa.Boolean(), server_default=sa.text("true"))),
            ("payment_updates", sa.Column("payment_updates", sa.Boolean(), server_default=sa.text("true"))),
            ("shipping_updates", sa.Column("shipping_updates", sa.Boolean(), server_default=sa.text("true"))),
            ("promotional_notifications", sa.Column("promotional_notifications", sa.Boolean(), server_default=sa.text("true"))),
            ("support_updates", sa.Column("support_updates", sa.Boolean(), server_default=sa.text("true"))),
            ("security_notifications", sa.Column("security_notifications", sa.Boolean(), server_default=sa.text("true"))),
            ("system_notifications", sa.Column("system_notifications", sa.Boolean(), server_default=sa.text("true"))),
            ("status", sa.Column("status", sa.String(64), server_default="active")),
        ):
            _add_column("notification_preferences", name, column)

    if "web_push_subscriptions" not in tables:
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
    else:
        for name, column in (
            ("user_id", sa.Column("user_id", postgresql.UUID(as_uuid=True))),
            ("endpoint", sa.Column("endpoint", sa.Text())),
            ("p256dh", sa.Column("p256dh", sa.Text())),
            ("auth", sa.Column("auth", sa.Text())),
            ("browser", sa.Column("browser", sa.String(120))),
            ("user_agent", sa.Column("user_agent", sa.Text())),
            ("status", sa.Column("status", sa.String(64), server_default="active")),
            ("is_active", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"))),
            ("last_success_at", sa.Column("last_success_at", sa.DateTime(timezone=True))),
            ("last_failure_at", sa.Column("last_failure_at", sa.DateTime(timezone=True))),
            ("failure_count", sa.Column("failure_count", sa.Integer(), server_default="0")),
        ):
            _add_column("web_push_subscriptions", name, column)

    if "notification_delivery_attempts" not in tables:
        op.create_table(
            "notification_delivery_attempts",
            *_resource_columns(),
            sa.Column("notification_id", postgresql.UUID(as_uuid=True)),
            sa.Column("user_id", postgresql.UUID(as_uuid=True)),
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
    else:
        for name, column in (
            ("notification_id", sa.Column("notification_id", postgresql.UUID(as_uuid=True))),
            ("user_id", sa.Column("user_id", postgresql.UUID(as_uuid=True))),
            ("channel", sa.Column("channel", sa.String(80))),
            ("target", sa.Column("target", sa.Text())),
            ("provider", sa.Column("provider", sa.String(80))),
            ("status", sa.Column("status", sa.String(64))),
            ("response_code", sa.Column("response_code", sa.String(80))),
            ("error_code", sa.Column("error_code", sa.String(160))),
            ("attempt_number", sa.Column("attempt_number", sa.Integer(), server_default="1")),
            ("sent_at", sa.Column("sent_at", sa.DateTime(timezone=True))),
            ("delivered_at", sa.Column("delivered_at", sa.DateTime(timezone=True))),
            ("failed_at", sa.Column("failed_at", sa.DateTime(timezone=True))),
        ):
            _add_column("notification_delivery_attempts", name, column)

    _create_index(
        "ux_notification_preferences_user",
        "notification_preferences",
        ["user_id"],
        unique=True,
        where="deleted_at IS NULL",
    )
    _create_index(
        "ux_web_push_endpoint_active",
        "web_push_subscriptions",
        ["endpoint"],
        unique=True,
        where="is_active IS TRUE",
    )
    _create_index("ix_notification_delivery_attempts_notification_id", "notification_delivery_attempts", ["notification_id"])


def downgrade() -> None:
    pass
