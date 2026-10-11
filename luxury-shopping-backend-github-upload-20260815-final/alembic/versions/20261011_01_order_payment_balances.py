"""Reconcile order balances from confirmed ledgers without changing payments."""

from alembic import op
import sqlalchemy as sa

revision = "20261011_01"
down_revision = "20260924_02"
branch_labels = None
depends_on = None


def _receipt_ref(alias: str) -> str:
    fields = [f"NULLIF({alias}.extra_data->>'payment_receipt_id', '')"]
    fields.extend(
        f"NULLIF(REPLACE({alias}.extra_data->>'{key}', 'receipt:', ''), '')"
        for key in ("receipt_url", "receiptUrl", "receiptPath")
    )
    return "LOWER(COALESCE(" + ", ".join(fields) + "))"


def upgrade() -> None:
    manual_ref, legacy_ref = _receipt_ref("m"), _receipt_ref("p")
    op.execute(sa.text(f"""
        WITH confirmed AS (
            SELECT order_id, amount FROM order_payments
            WHERE deleted_at IS NULL AND LOWER(status) IN ('confirmed', 'approved', 'paid')
            UNION ALL
            SELECT p.order_id, p.amount FROM payments p
            WHERE p.deleted_at IS NULL
              AND LOWER(p.status) IN ('confirmed', 'approved', 'paid', 'completed')
              AND NOT EXISTS (
                  SELECT 1 FROM order_payments m
                  WHERE m.order_id = p.order_id AND m.deleted_at IS NULL
                    AND LOWER(m.status) IN ('confirmed', 'approved', 'paid')
                    AND {manual_ref} = {legacy_ref}
              )
        ), paid AS (
            SELECT order_id, ROUND(COALESCE(SUM(amount), 0), 2) AS amount
            FROM confirmed GROUP BY order_id
        ), reconciled AS (
            SELECT o.id, COALESCE(p.amount, 0.00) AS paid_amount,
                   GREATEST(o.total - COALESCE(p.amount, 0.00), 0.00) AS remaining_balance,
                   CASE
                     WHEN o.payment_status IN ('refunded', 'partial_refund', 'partially_refunded', 'rejected')
                       THEN o.payment_status
                     WHEN o.total > 0 AND COALESCE(p.amount, 0) >= o.total THEN 'paid'
                     WHEN COALESCE(p.amount, 0) > 0 THEN 'partial'
                     ELSE 'pending'
                   END AS payment_status
            FROM orders o LEFT JOIN paid p ON p.order_id = o.id
            WHERE o.deleted_at IS NULL
        )
        UPDATE orders o SET
            extra_data = COALESCE(o.extra_data, '{{}}'::jsonb)
                || CASE WHEN COALESCE(o.extra_data, '{{}}'::jsonb) ? 'payment_balance_before_20261011'
                   THEN '{{}}'::jsonb
                   ELSE jsonb_build_object('payment_balance_before_20261011', jsonb_build_object(
                       'paid_amount', o.extra_data->'paid_amount',
                       'remaining_balance', o.extra_data->'remaining_balance',
                       'payment_status', o.payment_status)) END
                || jsonb_build_object('paid_amount', r.paid_amount::text,
                                      'remaining_balance', r.remaining_balance::text),
            payment_status = r.payment_status
        FROM reconciled r WHERE o.id = r.id
          AND (o.extra_data->>'paid_amount' IS DISTINCT FROM r.paid_amount::text
            OR o.extra_data->>'remaining_balance' IS DISTINCT FROM r.remaining_balance::text
            OR o.payment_status IS DISTINCT FROM r.payment_status)
    """))


def downgrade() -> None:
    # Correct derived balances remain valid when reverting application code.
    # Restoring obsolete balances would overwrite later payment activity.
    pass
