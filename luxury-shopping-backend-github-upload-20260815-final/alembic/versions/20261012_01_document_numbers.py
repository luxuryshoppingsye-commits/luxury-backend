"""Short daily references; keep former references and all internal identifiers."""

from alembic import op
import sqlalchemy as sa

revision = "20261012_01"
down_revision = "20261011_01"
branch_labels = None
depends_on = None

ORDERS = {"orders": "ORD", "international_orders": "INL", "local_shopping_requests": "LOC"}
VOUCHERS = {
    "order_payments": "RV", "payments": "RV", "international_order_payments": "RV",
    "employee_payments": "PV", "marketer_payments": "PV", "partner_payments": "PV",
    "general_expenses": "PV", "refunds": "PV",
    "financial_vouchers": None, "vouchers": None,
}
OTHER = {"international_purchases": ("purchase_number", "PUR"), "support_tickets": ("ticket_number", "SUP")}


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("document_number_counters"):
        op.create_table(
            "document_number_counters",
            sa.Column("prefix", sa.String(8), primary_key=True),
            sa.Column("number_date", sa.Date(), primary_key=True),
            sa.Column("last_value", sa.Integer(), nullable=False),
            sa.CheckConstraint("last_value > 0", name="ck_document_counter_positive"),
        )
    tables = list(ORDERS) + list(VOUCHERS) + list(OTHER)
    op.execute(sa.text("LOCK TABLE " + ", ".join(tables) + " IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(sa.text("""
        CREATE TEMP TABLE document_number_map (
            table_name text, record_id uuid, field text, prefix text,
            number_date date, created_at timestamptz, previous_number text,
            sequence bigint, new_number text
        ) ON COMMIT DROP
    """))
    for table in tables:
        date = "(COALESCE(created_at, CURRENT_TIMESTAMP) AT TIME ZONE 'Asia/Aden')::date"
        if table in ORDERS:
            previous = "order_number" if table != "local_shopping_requests" else "COALESCE(extra_data->>'order_number', 'LS-' || upper(substr(id::text, 1, 8)))"
            fields = [("order_number", f"'{ORDERS[table]}'", previous),
                      ("invoice_number", "'INV'", f"COALESCE(extra_data->>'invoice_number', {previous})")]
        elif table in VOUCHERS:
            prefix = f"'{VOUCHERS[table]}'" if VOUCHERS[table] else "CASE WHEN lower(COALESCE(extra_data->>'voucher_type', type, '')) = 'payment' THEN 'PV' ELSE 'RV' END"
            fields = [("voucher_number", prefix, "extra_data->>'voucher_number'")]
        else:
            field, prefix = OTHER[table]
            fields = [(field, f"'{prefix}'", f"extra_data->>'{field}'")]
        for field, prefix, previous in fields:
            op.execute(sa.text(f"""
                INSERT INTO document_number_map (table_name, record_id, field, prefix, number_date, created_at, previous_number)
                SELECT '{table}', id, '{field}', {prefix}, {date}, created_at, {previous} FROM {table}
            """))
    op.execute(sa.text("""
        WITH numbered AS (
            SELECT table_name, record_id, field,
                   COALESCE(c.last_value, 0) + row_number() OVER (
                       PARTITION BY m.prefix, m.number_date ORDER BY created_at NULLS FIRST, record_id, table_name, field
                   ) AS seq
            FROM document_number_map m LEFT JOIN document_number_counters c
                ON c.prefix = m.prefix AND c.number_date = m.number_date
        )
        UPDATE document_number_map m SET sequence = n.seq,
            new_number = m.prefix || '-' || to_char(m.number_date, 'YYMMDD') || '-' ||
                CASE WHEN n.seq < 10000 THEN lpad(n.seq::text, 4, '0') ELSE n.seq::text END
        FROM numbered n WHERE m.table_name = n.table_name AND m.record_id = n.record_id AND m.field = n.field
    """))
    op.execute(sa.text("""
        INSERT INTO document_number_counters (prefix, number_date, last_value)
        SELECT prefix, number_date, max(sequence) FROM document_number_map GROUP BY prefix, number_date
        ON CONFLICT (prefix, number_date) DO UPDATE SET last_value = EXCLUDED.last_value
    """))
    for table in tables:
        fields = ["order_number", "invoice_number"] if table in ORDERS else ["voucher_number" if table in VOUCHERS else OTHER[table][0]]
        for field in fields:
            column = field == "order_number" and table in {"orders", "international_orders"}
            value = "'{}'::jsonb" if column else f"jsonb_build_object('{field}', m.new_number)"
            column_update = "order_number = m.new_number," if column else ""
            op.execute(sa.text(f"""
                UPDATE {table} r SET {column_update}
                    extra_data = COALESCE(r.extra_data, '{{}}'::jsonb) || {value} ||
                        jsonb_build_object('numbers_before_20261012',
                            COALESCE(r.extra_data->'numbers_before_20261012', '{{}}'::jsonb) ||
                            jsonb_build_object('{field}', m.previous_number)) ||
                        CASE WHEN m.previous_number IS NOT NULL AND NOT COALESCE(r.extra_data, '{{}}'::jsonb) ? 'previous_{field}'
                             THEN jsonb_build_object('previous_{field}', m.previous_number) ELSE '{{}}'::jsonb END
                FROM document_number_map m
                WHERE m.table_name = '{table}' AND m.field = '{field}' AND r.id = m.record_id
            """))
            if not column:
                op.execute(sa.text(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_{field} ON {table} ((extra_data->>'{field}'))"))


def downgrade():
    # Retain published references and counters; rollback must not reuse numbers.
    pass
