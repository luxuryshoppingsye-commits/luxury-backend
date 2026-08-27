"""Add the fields required by the secure product-review flow."""

from __future__ import annotations

from alembic import op


revision = "20260826_01"
down_revision = "20260817_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older installations created product_reviews from the generic resource
    # registry, so these columns must be added explicitly for existing data.
    op.execute(
        """
        ALTER TABLE public.product_reviews
            ADD COLUMN IF NOT EXISTS order_id UUID,
            ADD COLUMN IF NOT EXISTS rating INTEGER,
            ADD COLUMN IF NOT EXISTS comment TEXT,
            ADD COLUMN IF NOT EXISTS review_images JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS is_verified_purchase BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )
    op.execute(
        """
        UPDATE public.product_reviews
        SET is_approved = TRUE
        WHERE is_approved = FALSE
          AND lower(coalesce(status, '')) IN ('approved', 'active', 'published', 'visible', 'live');
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'ck_product_reviews_rating_range'
            ) THEN
                ALTER TABLE public.product_reviews
                    ADD CONSTRAINT ck_product_reviews_rating_range
                    CHECK (rating IS NULL OR rating BETWEEN 1 AND 5);
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_product_reviews_product_public
        ON public.product_reviews (product_id, status, is_approved, deleted_at, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_product_reviews_user_product
        ON public.product_reviews (user_id, product_id, deleted_at);
        """
    )


def downgrade() -> None:
    # Keep review data on rollback. The next forward migration can safely
    # continue using the columns and indexes through IF NOT EXISTS guards.
    pass
