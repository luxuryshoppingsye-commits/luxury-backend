"""Ensure durable refresh-token sessions exist for remembered logins.

Revision ID: 20260908_01
Revises: 20260826_01
Create Date: 2026-09-08
"""
from __future__ import annotations

from alembic import op

from app.models import Base


revision = "20260908_01"
down_revision = "20260826_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older live databases can have the Alembic revision history without this
    # durable-session table.  The auth service deliberately avoids issuing a
    # refresh token in that state, so create it idempotently before login.
    Base.metadata.tables["refresh_tokens"].create(
        bind=op.get_bind(),
        checkfirst=True,
    )


def downgrade() -> None:
    # A remembered session can be active.  Keep the table and its security
    # history if this migration is rolled back.
    pass
