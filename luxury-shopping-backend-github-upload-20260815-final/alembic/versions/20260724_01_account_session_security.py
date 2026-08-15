"""Add account and session security state.

Revision ID: 20260724_01
Revises: 20260723_01
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models import Base


revision: str = "20260724_01"
down_revision: Union[str, Sequence[str], None] = "20260723_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.tables["account_security"].create(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["refresh_token_security"].create(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["password_reset_token_state"].create(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["verification_tokens"].create(bind=op.get_bind(), checkfirst=True)
    Base.metadata.tables["phone_otp_tokens"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.drop_table("phone_otp_tokens")
    op.drop_table("verification_tokens")
    op.drop_table("password_reset_token_state")
    op.drop_table("refresh_token_security")
    op.drop_table("account_security")
