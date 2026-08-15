"""Add partial indexes for the public catalogue read paths."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260806_02"
down_revision = "20260806_01"
branch_labels = None
depends_on = None


# Keep the predicate identical to the stable part of the public catalogue
# policy. Approval is intentionally evaluated in application SQL because the
# policy accepts several legacy/public values; indexing only non-deleted rows
# remains safe and lets PostgreSQL filter the approval expression cheaply.
_PUBLIC_WHERE = sa.text("deleted_at IS NULL")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "products" in tables:
        indexes = {item["name"] for item in inspector.get_indexes("products")}
        definitions = (
            (
                "ix_products_public_created_id",
                ["created_at", "id"],
            ),
            (
                "ix_products_public_category_created",
                ["category_id", "created_at", "id"],
            ),
            (
                "ix_products_public_brand_created",
                ["brand_id", "created_at", "id"],
            ),
        )
        for name, columns in definitions:
            if name not in indexes:
                op.create_index(
                    name,
                    "products",
                    columns,
                    unique=False,
                    postgresql_where=_PUBLIC_WHERE,
                )

    if "product_variants" in tables:
        indexes = {item["name"] for item in inspector.get_indexes("product_variants")}
        if "ix_product_variants_public_product_sort" not in indexes:
            op.create_index(
                "ix_product_variants_public_product_sort",
                "product_variants",
                ["product_id", "sort_order", "created_at"],
                unique=False,
                postgresql_where=sa.text("deleted_at IS NULL AND is_active IS TRUE"),
            )


def downgrade() -> None:
    for name, table in (
        ("ix_product_variants_public_product_sort", "product_variants"),
        ("ix_products_public_brand_created", "products"),
        ("ix_products_public_category_created", "products"),
        ("ix_products_public_created_id", "products"),
    ):
        inspector = sa.inspect(op.get_bind())
        if table in set(inspector.get_table_names()):
            indexes = {item["name"] for item in inspector.get_indexes(table)}
            if name in indexes:
                op.drop_index(name, table_name=table)
