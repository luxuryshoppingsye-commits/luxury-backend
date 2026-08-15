"""Add report exports and normalize frequently queried resource columns.

Revision ID: 20260719_02
Revises: 20260719_01
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


revision: str = "20260719_02"
down_revision: Union[str, Sequence[str], None] = "20260719_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _jsonb_to_text(table: str, column: str) -> None:
    columns = {item["name"]: item["type"] for item in inspect(op.get_bind()).get_columns(table)}
    if column not in columns or not isinstance(columns[column], JSONB):
        return
    op.execute(
        f'''ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TEXT
        USING CASE WHEN "{column}" IS NULL THEN NULL ELSE "{column}" #>> '{{}}' END'''
    )


def upgrade() -> None:
    Base.metadata.tables["report_exports"].create(bind=op.get_bind(), checkfirst=True)
    _jsonb_to_text("account_deletion_requests", "reason")
    for table in ("notification_outbox", "email_outbox", "whatsapp_outbox"):
        if "target" in Base.metadata.tables[table].c:
            _jsonb_to_text(table, "target")
        if "channel" in Base.metadata.tables[table].c:
            _jsonb_to_text(table, "channel")


def downgrade() -> None:
    pass
