from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260720_05"
down_revision = "20260720_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "customer_addresses" not in inspector.get_table_names():
        op.create_table(
            "customer_addresses",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("label", sa.String(length=240), nullable=True),
            sa.Column("recipient_name", sa.String(length=240), nullable=True),
            sa.Column("phone", sa.String(length=32), nullable=True),
            sa.Column("governorate", sa.String(length=160), nullable=True),
            sa.Column("city", sa.String(length=160), nullable=True),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
            sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
            sa.Column(
                "is_default",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=True,
            ),
            sa.Column(
                "extra_data",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default="{}",
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    index_names = {index["name"] for index in inspector.get_indexes("customer_addresses")}
    if "ix_customer_addresses_user_id" not in index_names:
        op.create_index("ix_customer_addresses_user_id", "customer_addresses", ["user_id"], unique=False)
    if "ix_customer_addresses_label" not in index_names:
        op.create_index("ix_customer_addresses_label", "customer_addresses", ["label"], unique=False)
    if "ix_customer_addresses_recipient_name" not in index_names:
        op.create_index("ix_customer_addresses_recipient_name", "customer_addresses", ["recipient_name"], unique=False)
    if "ix_customer_addresses_phone" not in index_names:
        op.create_index("ix_customer_addresses_phone", "customer_addresses", ["phone"], unique=False)
    if "ix_customer_addresses_governorate" not in index_names:
        op.create_index("ix_customer_addresses_governorate", "customer_addresses", ["governorate"], unique=False)
    if "uq_customer_addresses_one_default_per_user" not in index_names:
        op.create_index(
            "uq_customer_addresses_one_default_per_user",
            "customer_addresses",
            ["user_id"],
            unique=True,
            postgresql_where=sa.text("is_default IS TRUE AND deleted_at IS NULL"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_customer_addresses_one_default_per_user",
        table_name="customer_addresses",
    )
    op.drop_index("ix_customer_addresses_governorate", table_name="customer_addresses")
    op.drop_index("ix_customer_addresses_phone", table_name="customer_addresses")
    op.drop_index("ix_customer_addresses_recipient_name", table_name="customer_addresses")
    op.drop_index("ix_customer_addresses_label", table_name="customer_addresses")
    op.drop_index("ix_customer_addresses_user_id", table_name="customer_addresses")
    op.drop_table("customer_addresses")
