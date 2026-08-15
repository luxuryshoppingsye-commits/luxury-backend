from __future__ import annotations

from alembic import op


revision = "20260720_01"
down_revision = "20260719_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_search_name_trgm
        ON products USING gin (name gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_search_name_en_trgm
        ON products USING gin (name_en gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_search_description_trgm
        ON products USING gin (description gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_search_sku_trgm
        ON products USING gin (sku gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_public_created
        ON products (created_at DESC)
        WHERE deleted_at IS NULL
          AND is_active IS TRUE
          AND approval_status IN ('approved', 'active')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_products_public_price
        ON products (price)
        WHERE deleted_at IS NULL
          AND is_active IS TRUE
          AND approval_status IN ('approved', 'active')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_products_public_price")
    op.execute("DROP INDEX IF EXISTS ix_products_public_created")
    op.execute("DROP INDEX IF EXISTS ix_products_search_sku_trgm")
    op.execute("DROP INDEX IF EXISTS ix_products_search_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_products_search_name_en_trgm")
    op.execute("DROP INDEX IF EXISTS ix_products_search_name_trgm")
