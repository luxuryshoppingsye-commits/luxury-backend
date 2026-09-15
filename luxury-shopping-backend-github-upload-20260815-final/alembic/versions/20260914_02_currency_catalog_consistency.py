"""Ensure the built-in currency catalog is shared by the app and dashboard."""

from alembic import op


revision = "20260914_02"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


_SAR_ID = "6b6f5e9b-3f3e-4b8e-9a0e-7c8b9b0c4d21"


def upgrade() -> None:
    # Currency metadata is stored in extra_data by the compatibility schema.
    # Preserve existing values when present, while filling the fields used by
    # both the Flutter client and the web dashboard.
    op.execute(
        """
        WITH deleted_sar AS (
            SELECT id
            FROM public.currencies
            WHERE upper(btrim(code)) = 'SAR'
              AND deleted_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.currencies AS active_sar
                  WHERE upper(btrim(active_sar.code)) = 'SAR'
                    AND active_sar.deleted_at IS NULL
              )
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
        )
        UPDATE public.currencies AS currencies
        SET deleted_at = NULL,
            name = 'ريال سعودي',
            name_en = 'Saudi Riyal',
            status = 'active',
            is_active = TRUE,
            extra_data = COALESCE(currencies.extra_data, '{}'::jsonb)
                || jsonb_build_object(
                    'symbol', COALESCE(currencies.extra_data->'symbol', to_jsonb('ر.س'::text)),
                    'exchange_rate', COALESCE(currencies.extra_data->'exchange_rate', to_jsonb(0.0071::numeric)),
                    'sort_order', COALESCE(currencies.extra_data->'sort_order', to_jsonb(2))
                )
        WHERE currencies.id IN (SELECT id FROM deleted_sar);
        """
    )
    op.execute(
        """
        UPDATE public.currencies
        SET name = 'ريال سعودي',
            name_en = 'Saudi Riyal',
            status = 'active',
            is_active = TRUE,
            extra_data = COALESCE(extra_data, '{}'::jsonb)
                || jsonb_build_object(
                    'symbol', COALESCE(extra_data->'symbol', to_jsonb('ر.س'::text)),
                    'exchange_rate', COALESCE(extra_data->'exchange_rate', to_jsonb(0.0071::numeric)),
                    'sort_order', COALESCE(extra_data->'sort_order', to_jsonb(2))
                )
        WHERE upper(btrim(code)) = 'SAR'
          AND deleted_at IS NULL;
        """
    )
    op.execute(
        f"""
        INSERT INTO public.currencies (
            id,
            created_at,
            updated_at,
            name,
            name_en,
            code,
            status,
            is_active,
            extra_data
        )
        SELECT '{_SAR_ID}'::uuid,
               CURRENT_TIMESTAMP,
               CURRENT_TIMESTAMP,
               'ريال سعودي',
               'Saudi Riyal',
               'SAR',
               'active',
               TRUE,
               jsonb_build_object(
                   'symbol', 'ر.س',
                   'exchange_rate', 0.0071,
                   'is_default', FALSE,
                   'sort_order', 2
               )
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.currencies
            WHERE upper(btrim(code)) = 'SAR'
              AND deleted_at IS NULL
        );
        """
    )


def downgrade() -> None:
    # Keep the catalog record on rollback; removing a currency can invalidate
    # prices and historical orders that already reference its code.
    pass
