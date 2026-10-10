"""Store the merchant's private product purchase cost separately from the compare-at price."""

from alembic import op
import re
import sqlalchemy as sa


revision = "20260924_02"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("products")}
    cost = columns.get("cost_price")
    if cost is None:
        op.add_column("products", sa.Column("cost_price", sa.Numeric(18, 2), nullable=True))
    elif not (
        isinstance(cost["type"], sa.Numeric)
        and cost["type"].precision == 18 and cost["type"].scale == 2 and cost["nullable"]
    ):
        raise RuntimeError("Existing products.cost_price has an incompatible definition")
    constraint = next((item for item in inspector.get_check_constraints("products") if item["name"] == "ck_products_cost_price_positive"), None)
    if constraint is None:
        op.create_check_constraint("ck_products_cost_price_positive", "products", "cost_price IS NULL OR cost_price > 0")
    else:
        expression = re.sub(r"::numeric", "", constraint["sqltext"].lower())
        expression = re.sub(r"[\s()\"']", "", expression)
        if expression not in {"cost_priceisnullorcost_price>0", "cost_price>0orcost_priceisnull"}:
            raise RuntimeError("Existing product cost constraint does not match the positive cost guard")
        op.execute("ALTER TABLE products VALIDATE CONSTRAINT ck_products_cost_price_positive")


def downgrade() -> None:
    op.drop_constraint("ck_products_cost_price_positive", "products", type_="check")
    op.drop_column("products", "cost_price")
