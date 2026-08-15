from __future__ import annotations

from alembic import op


revision = "20260720_03"
down_revision = "20260720_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (
        "order_payments",
        "payments",
        "payment_receipts",
        "refunds",
        "partner_payments",
        "marketer_commissions",
        "marketer_payments",
        "partner_settlements",
        "order_financials",
        "financial_vouchers",
        "cash_transactions",
        "vouchers",
    ):
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT ck_financial_{table}_amount_nonnegative
            CHECK (amount IS NULL OR amount >= 0)
            NOT VALID
            """
        )
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_financial_{table}_amount_nonnegative")

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_refunds_idempotency_scope
        ON refunds (
            (extra_data->>'idempotency_actor_id'),
            (extra_data->>'idempotency_endpoint'),
            (extra_data->>'idempotency_key')
        )
        WHERE extra_data ? 'idempotency_key'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_financial_receipts_order_status
        ON payment_receipts (order_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_financial_refunds_order_status
        ON refunds (order_id, status)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_financial_refunds_order_status")
    op.execute("DROP INDEX IF EXISTS ix_financial_receipts_order_status")
    op.execute("DROP INDEX IF EXISTS uq_refunds_idempotency_scope")
    for table in (
        "vouchers",
        "cash_transactions",
        "financial_vouchers",
        "order_financials",
        "partner_settlements",
        "marketer_payments",
        "marketer_commissions",
        "partner_payments",
        "refunds",
        "payment_receipts",
        "payments",
        "order_payments",
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_financial_{table}_amount_nonnegative")
