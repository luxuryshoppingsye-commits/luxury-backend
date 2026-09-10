"""Add durable server-side sessions for remembered mobile logins.

Revision ID: 20260909_01
Revises: 20260908_02
Create Date: 2026-09-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models import Base


revision = "20260909_01"
down_revision = "20260908_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Create the parent table before adding the optional link from the
    # rotating refresh-token table. This is safe for older databases because
    # every operation is idempotent.
    Base.metadata.tables["auth_sessions"].create(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    refresh_columns = {column["name"] for column in inspector.get_columns("refresh_tokens")}
    if "session_id" not in refresh_columns:
        op.add_column(
            "refresh_tokens",
            sa.Column(
                "session_id",
                sa.UUID(),
                sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_refresh_tokens_session_id",
            "refresh_tokens",
            ["session_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    refresh_columns = {column["name"] for column in inspector.get_columns("refresh_tokens")}
    if "session_id" in refresh_columns:
        op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
        op.drop_constraint(
            "refresh_tokens_session_id_fkey",
            "refresh_tokens",
            type_="foreignkey",
        )
        op.drop_column("refresh_tokens", "session_id")
    Base.metadata.tables["auth_sessions"].drop(bind=bind, checkfirst=True)
