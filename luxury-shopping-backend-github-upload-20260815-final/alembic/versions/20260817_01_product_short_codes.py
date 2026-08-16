"""Backfill and automatically generate compact product share codes."""

from __future__ import annotations

from alembic import op


revision = "20260817_01"
down_revision = "20260811_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.generate_product_short_code()
        RETURNS varchar
        LANGUAGE plpgsql
        AS $$
        DECLARE
            alphabet constant text := 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
            candidate text;
            index_value integer;
        BEGIN
            LOOP
                candidate := '';
                FOR index_value IN 1..6 LOOP
                    candidate := candidate || substr(
                        alphabet,
                        (floor(random() * length(alphabet))::integer + 1),
                        1
                    );
                END LOOP;
                IF NOT EXISTS (
                    SELECT 1 FROM public.products WHERE short_code = candidate
                ) THEN
                    RETURN candidate;
                END IF;
            END LOOP;
        END;
        $$;
        """
    )

    op.execute(
        """
        DO $$
        DECLARE
            duplicate_row record;
            duplicate_product record;
        BEGIN
            FOR duplicate_row IN
                SELECT short_code
                FROM public.products
                WHERE short_code IS NOT NULL AND btrim(short_code) <> ''
                GROUP BY short_code
                HAVING count(*) > 1
            LOOP
                FOR duplicate_product IN
                    SELECT id
                    FROM public.products
                    WHERE short_code = duplicate_row.short_code
                    ORDER BY id
                    OFFSET 1
                LOOP
                    UPDATE public.products
                    SET short_code = public.generate_product_short_code()
                    WHERE id = duplicate_product.id;
                END LOOP;
            END LOOP;

            UPDATE public.products
            SET short_code = public.generate_product_short_code()
            WHERE short_code IS NULL OR btrim(short_code) = '';
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_products_short_code
        ON public.products (short_code)
        WHERE short_code IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.assign_product_short_code()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.short_code IS NULL OR btrim(NEW.short_code) = '' THEN
                NEW.short_code := public.generate_product_short_code();
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_products_assign_short_code ON public.products")
    op.execute(
        """
        CREATE TRIGGER trg_products_assign_short_code
        BEFORE INSERT ON public.products
        FOR EACH ROW
        EXECUTE FUNCTION public.assign_product_short_code();
        """
    )


def downgrade() -> None:
    # Keep generated codes and the unique index in place so old shared links
    # remain valid if migrations are rolled back.
    pass
