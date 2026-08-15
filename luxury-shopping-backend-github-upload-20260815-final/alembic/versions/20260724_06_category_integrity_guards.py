from __future__ import annotations

from alembic import op


revision = "20260724_06"
down_revision = "20260724_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM categories
                WHERE deleted_at IS NULL
                GROUP BY lower(btrim(name))
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'active duplicate category names must be remediated before category integrity guards are installed';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM categories
                WHERE deleted_at IS NULL
                  AND name_en IS NOT NULL
                  AND btrim(name_en) <> ''
                GROUP BY lower(btrim(name_en))
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'active duplicate category English names must be remediated before category integrity guards are installed';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM categories
                WHERE deleted_at IS NULL
                  AND slug IS NOT NULL
                  AND btrim(slug) <> ''
                GROUP BY lower(btrim(slug))
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'active duplicate category slugs must be remediated before category integrity guards are installed';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_categories_name_not_blank'
            ) THEN
                ALTER TABLE categories
                ADD CONSTRAINT ck_categories_name_not_blank
                CHECK (btrim(name) <> '') NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE categories VALIDATE CONSTRAINT ck_categories_name_not_blank")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_categories_parent_not_self'
            ) THEN
                ALTER TABLE categories
                ADD CONSTRAINT ck_categories_parent_not_self
                CHECK (parent_id IS NULL OR parent_id <> id) NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE categories VALIDATE CONSTRAINT ck_categories_parent_not_self")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_active_name_normalized
        ON categories ((lower(btrim(name))))
        WHERE deleted_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_active_slug_normalized
        ON categories ((lower(btrim(slug))))
        WHERE deleted_at IS NULL
          AND slug IS NOT NULL
          AND btrim(slug) <> '';
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_active_name_en_normalized
        ON categories ((lower(btrim(name_en))))
        WHERE deleted_at IS NULL
          AND name_en IS NOT NULL
          AND btrim(name_en) <> '';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_categories_active_name_en_normalized")
    op.execute("DROP INDEX IF EXISTS ux_categories_active_slug_normalized")
    op.execute("DROP INDEX IF EXISTS ux_categories_active_name_normalized")
    op.execute("ALTER TABLE categories DROP CONSTRAINT IF EXISTS ck_categories_parent_not_self")
    op.execute("ALTER TABLE categories DROP CONSTRAINT IF EXISTS ck_categories_name_not_blank")
