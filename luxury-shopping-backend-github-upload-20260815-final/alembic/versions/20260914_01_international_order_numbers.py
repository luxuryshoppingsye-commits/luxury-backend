"""Add searchable numbers to international shopping orders."""

from alembic import op


revision = "20260914_01"
down_revision = "20260909_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.international_orders
            ADD COLUMN IF NOT EXISTS order_number VARCHAR(80);
        """
    )
    op.execute(
        """
        UPDATE public.international_orders
        SET order_number = 'INTL-' ||
            to_char(COALESCE(created_at, CURRENT_TIMESTAMP), 'YYYYMMDD') || '-' ||
            upper(replace(id::text, '-', ''))
        WHERE order_number IS NULL OR btrim(order_number) = '';
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY order_number
                       ORDER BY created_at NULLS FIRST, id
                   ) AS duplicate_rank
            FROM public.international_orders
            WHERE order_number IS NOT NULL AND btrim(order_number) <> ''
        )
        UPDATE public.international_orders AS orders
        SET order_number = 'INTL-' ||
            to_char(COALESCE(orders.created_at, CURRENT_TIMESTAMP), 'YYYYMMDD') || '-' ||
            upper(replace(orders.id::text, '-', ''))
        FROM ranked
        WHERE orders.id = ranked.id AND ranked.duplicate_rank > 1;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_international_orders_order_number
        ON public.international_orders (order_number);
        """
    )
    op.execute(
        """
        ALTER TABLE public.international_orders
            ALTER COLUMN order_number SET NOT NULL;
        """
    )


def downgrade() -> None:
    # Keep the numbers on rollback; they are customer-facing references.
    pass
