"""Add explicit per-staff dashboard permission sets."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260811_01"
down_revision = "20260807_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "staff_permission_sets" in set(inspector.get_table_names()):
        return
    op.create_table(
        "staff_permission_sets",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    # Keep this migration non-destructive for production data.
    pass
