"""Add a composite index for public product review aggregates."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260807_01"
down_revision = "20260806_02"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_product_reviews_public_product_status_deleted"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "product_reviews" not in tables:
        return

    columns = {item["name"] for item in inspector.get_columns("product_reviews")}
    indexes = {item["name"] for item in inspector.get_indexes("product_reviews")}
    required = {"product_id", "status", "deleted_at"}
    if INDEX_NAME not in indexes and required.issubset(columns):
        op.create_index(
            INDEX_NAME,
            "product_reviews",
            ["product_id", "status", "deleted_at"],
            unique=False,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "product_reviews" not in set(inspector.get_table_names()):
        return
    indexes = {item["name"] for item in inspector.get_indexes("product_reviews")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="product_reviews")
