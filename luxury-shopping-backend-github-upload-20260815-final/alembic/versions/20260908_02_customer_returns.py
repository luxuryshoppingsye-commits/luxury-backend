"""Add customer return requests and return item snapshots.

Revision ID: 20260908_02
Revises: 20260908_01
Create Date: 2026-09-08
"""
from __future__ import annotations

from alembic import op

from app.models import Base


revision = "20260908_02"
down_revision = "20260908_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["returns"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["return_items"].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["return_items"].drop(bind=bind, checkfirst=True)
    Base.metadata.tables["returns"].drop(bind=bind, checkfirst=True)
