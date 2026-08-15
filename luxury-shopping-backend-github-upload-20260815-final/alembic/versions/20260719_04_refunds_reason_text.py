"""Normalize refund reason column type.

Revision ID: 20260719_04
Revises: 20260719_03
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "20260719_04"
down_revision: Union[str, Sequence[str], None] = "20260719_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {item["name"]: item["type"] for item in inspect(op.get_bind()).get_columns("refunds")}
    if isinstance(columns.get("reason"), JSONB):
        op.execute(
            """ALTER TABLE refunds ALTER COLUMN reason TYPE TEXT
            USING CASE
                WHEN reason IS NULL THEN NULL
                WHEN jsonb_typeof(reason) = 'string' THEN reason #>> '{}'
                ELSE reason::text
            END"""
        )


def downgrade() -> None:
    columns = {item["name"]: item["type"] for item in inspect(op.get_bind()).get_columns("refunds")}
    if columns.get("reason") is not None and not isinstance(columns.get("reason"), JSONB):
        op.execute("ALTER TABLE refunds ALTER COLUMN reason TYPE JSONB USING to_jsonb(reason)")
