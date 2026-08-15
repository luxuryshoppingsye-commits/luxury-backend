"""Ensure mobile push-token storage exists in production databases."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260803_01"
down_revision = "20260730_01"
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


def upgrade() -> None:
    tables = _tables()
    if "push_tokens" not in tables:
        op.create_table(
            "push_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("extra_data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token", sa.Text(), nullable=False),
            sa.Column("platform", sa.String(32), nullable=False, server_default="android"),
            sa.Column("device_id", sa.String(160)),
            sa.Column("app_version", sa.String(80)),
            sa.Column("device_name", sa.String(240)),
            sa.Column("environment", sa.String(80), server_default="production"),
            sa.Column("status", sa.String(64), server_default="active"),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("invalidated_at", sa.DateTime(timezone=True)),
            sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        )
    else:
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

    indexes = _indexes("push_tokens")
    if "ix_push_tokens_user_id" not in indexes:
        op.create_index("ix_push_tokens_user_id", "push_tokens", ["user_id"])
    if "ux_push_tokens_token_active" not in indexes:
        op.create_index(
            "ux_push_tokens_token_active",
            "push_tokens",
            ["token"],
            unique=True,
            postgresql_where=sa.text("token IS NOT NULL AND is_active IS TRUE"),
        )


def downgrade() -> None:
    # Production data is not removed by an automatic downgrade.
    pass
