"""Add secure file asset metadata.

Revision ID: 20260724_02
Revises: 20260724_01
Create Date: 2026-07-24
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models import Base


revision: str = "20260724_02"
down_revision: Union[str, Sequence[str], None] = "20260724_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.tables["file_assets"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    if "file_assets" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("file_assets")
