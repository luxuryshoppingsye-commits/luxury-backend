"""Assign memorable SKUs while retaining former automatic references."""
from alembic import op
import sqlalchemy as sa

revision = "20261123_01"
down_revision = "20261012_01"
branch_labels = None
depends_on = None

TABLES = (("products", "P"), ("product_variants", "V"))
AUTO_PATTERN = "^(LUX-PROD-[A-Z0-9]+-[0-9A-F]{32}|PROD-[A-Z0-9]{1,12}-[A-Z0-9]{1,12}-[0-9A-F]{8})$"


def upgrade():
    for table, prefix in TABLES:
        op.add_column(table, sa.Column("previous_auto_sku", sa.String(160)))
        op.execute(f"CREATE UNIQUE INDEX uq_{table}_previous_auto_sku ON {table} (previous_auto_sku) WHERE previous_auto_sku <> ''")
        op.execute(f"CREATE SEQUENCE {table}_sku_seq START WITH 1001")
        op.execute(f"""
            CREATE FUNCTION next_{table}_sku() RETURNS text LANGUAGE plpgsql AS $$
            DECLARE candidate text;
            BEGIN
                LOOP
                    candidate := '{prefix}' || nextval('{table}_sku_seq')::text;
                    EXIT WHEN NOT EXISTS (SELECT FROM {table} WHERE sku = candidate);
                END LOOP;
                RETURN candidate;
            END
            $$
        """)
        op.execute(f"""
            CREATE FUNCTION assign_{table}_sku() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF COALESCE(btrim(NEW.sku), '') = '' OR NEW.sku ~* '{AUTO_PATTERN}' THEN
                    IF TG_OP = 'UPDATE' AND COALESCE(btrim(OLD.sku), '') <> ''
                        AND OLD.sku !~* '{AUTO_PATTERN}' THEN
                        NEW.sku := OLD.sku;
                    ELSE
                        NEW.previous_auto_sku := COALESCE(NEW.sku, '');
                        NEW.sku := next_{table}_sku();
                    END IF;
                END IF;
                RETURN NEW;
            END
            $$
        """)
        op.execute(f"CREATE TRIGGER assign_short_sku BEFORE INSERT OR UPDATE OF sku ON {table} FOR EACH ROW EXECUTE FUNCTION assign_{table}_sku()")
        op.execute(f"UPDATE {table} SET sku = sku WHERE COALESCE(btrim(sku), '') = '' OR sku ~* '{AUTO_PATTERN}'")


def downgrade():
    for table, prefix in reversed(TABLES):
        op.execute(f"DROP TRIGGER assign_short_sku ON {table}")
        op.execute(f"UPDATE {table} SET sku = NULLIF(previous_auto_sku, '') WHERE previous_auto_sku IS NOT NULL AND sku ~ '^{prefix}[0-9]+$'")
        op.execute(f"DROP FUNCTION assign_{table}_sku()")
        op.execute(f"DROP FUNCTION next_{table}_sku()")
        op.execute(f"DROP SEQUENCE {table}_sku_seq")
        op.drop_column(table, "previous_auto_sku")
