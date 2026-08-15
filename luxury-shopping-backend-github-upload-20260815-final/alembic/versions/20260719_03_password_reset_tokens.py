"""Add secure password reset tokens.

Revision ID: 20260719_03
Revises: 20260719_02
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op

from app.models import Base


revision: str = "20260719_03"
down_revision: Union[str, Sequence[str], None] = "20260719_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.tables["password_reset_tokens"].create(
        bind=op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
