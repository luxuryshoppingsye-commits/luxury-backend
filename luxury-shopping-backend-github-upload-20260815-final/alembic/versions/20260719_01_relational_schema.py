"""Create the relational Luxury Shopping schema without deleting legacy data.

Revision ID: 20260719_01
Revises:
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op

from app.models import Base


revision: str = "20260719_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Deliberately non-destructive. Production data must only be removed by a
    # separately reviewed migration with a fresh pg_dump.
    pass
