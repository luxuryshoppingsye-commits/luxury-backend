"""Store the merchant's private product purchase cost separately from the compare-at price."""

from alembic import op
import sqlalchemy as sa


revision = "20260924_02"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("cost_price", sa.Numeric(18, 2), nullable=True))
    op.create_check_constraint("ck_products_cost_price_positive", "products", "cost_price IS NULL OR cost_price > 0")


def downgrade() -> None:
    op.drop_constraint("ck_products_cost_price_positive", "products", type_="check")
    op.drop_column("products", "cost_price")
