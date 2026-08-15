from __future__ import annotations

from alembic import op


revision = "20260720_02"
down_revision = "20260720_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id, product_id, COALESCE(variant_id, '00000000-0000-0000-0000-000000000000'::uuid)
                    ORDER BY created_at, id
                ) AS row_number,
                SUM(quantity) OVER (
                    PARTITION BY user_id, product_id, COALESCE(variant_id, '00000000-0000-0000-0000-000000000000'::uuid)
                ) AS total_quantity
            FROM user_cart
        ),
        merged AS (
            UPDATE user_cart AS cart
            SET quantity = ranked.total_quantity
            FROM ranked
            WHERE cart.id = ranked.id
              AND ranked.row_number = 1
            RETURNING cart.id
        )
        DELETE FROM user_cart AS cart
        USING ranked
        WHERE cart.id = ranked.id
          AND ranked.row_number > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_cart_line_without_variant
        ON user_cart (user_id, product_id)
        WHERE variant_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_cart_line_with_variant
        ON user_cart (user_id, product_id, variant_id)
        WHERE variant_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_orders_idempotency_user
        ON orders (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_orders_idempotency_user")
    op.execute("DROP INDEX IF EXISTS uq_user_cart_line_with_variant")
    op.execute("DROP INDEX IF EXISTS uq_user_cart_line_without_variant")
