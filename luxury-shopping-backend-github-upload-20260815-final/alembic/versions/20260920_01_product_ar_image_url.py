"""Add the transparent AR try-on image to products."""

from alembic import op


revision = "20260920_01"
down_revision = "20260914_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.products
            ADD COLUMN IF NOT EXISTS ar_image_url TEXT;
        """
    )
    op.execute(
        """
        UPDATE public.products
        SET ar_image_url = COALESCE(
            NULLIF(btrim(extra_data->>'ar_image_url'), ''),
            NULLIF(btrim(extra_data->>'arImageUrl'), '')
        )
        WHERE ar_image_url IS NULL
          AND extra_data IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.products
        SET extra_data = COALESCE(extra_data, '{}'::jsonb)
            || jsonb_build_object('ar_image_url', ar_image_url)
        WHERE ar_image_url IS NOT NULL
          AND btrim(ar_image_url) <> '';
        """
    )
    op.execute(
        """
        ALTER TABLE public.products
            DROP COLUMN IF EXISTS ar_image_url;
        """
    )
