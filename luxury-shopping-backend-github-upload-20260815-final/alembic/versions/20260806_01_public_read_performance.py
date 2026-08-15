"""Add indexes for the public-read and request-rate-limit hot paths."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260806_01"
down_revision = "20260803_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # The limiter counts recent rows by the HMAC key and timestamp.  The
    # existing single-column indexes cannot serve that predicate efficiently.
    if "security_events" in tables:
        indexes = {item["name"] for item in inspector.get_indexes("security_events")}
        if "ix_security_events_rate_limit_key_time" not in indexes:
            op.create_index(
                "ix_security_events_rate_limit_key_time",
                "security_events",
                ["type", "description", "created_at"],
                unique=False,
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "security_events" in set(inspector.get_table_names()):
        indexes = {item["name"] for item in inspector.get_indexes("security_events")}
        if "ix_security_events_rate_limit_key_time" in indexes:
            op.drop_index("ix_security_events_rate_limit_key_time", table_name="security_events")
