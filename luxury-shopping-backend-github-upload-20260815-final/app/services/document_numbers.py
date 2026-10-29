from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


ORDER_PREFIXES = {
    "orders": "ORD",
    "international_orders": "INL",
    "local_shopping_requests": "LOC",
}
VOUCHER_PREFIXES = {
    "order_payments": "RV",
    "payments": "RV",
    "international_order_payments": "RV",
    "employee_payments": "PV",
    "marketer_payments": "PV",
    "partner_payments": "PV",
    "general_expenses": "PV",
    "refunds": "PV",
}
NUMBER_TIMEZONE = timezone(timedelta(hours=3))


def next_document_number(connection, counters, prefix, number_date=None):
    number_date = number_date or datetime.now(NUMBER_TIMEZONE).date()
    insert = sqlite_insert if connection.dialect.name == "sqlite" else postgres_insert
    statement = insert(counters).values(prefix=prefix, number_date=number_date, last_value=1)
    statement = statement.on_conflict_do_update(
        index_elements=[counters.c.prefix, counters.c.number_date],
        set_={"last_value": counters.c.last_value + 1},
    ).returning(counters.c.last_value)
    sequence = connection.execute(statement).scalar_one()
    return f"{prefix}-{number_date:%y%m%d}-{sequence:04d}"


def register_document_numbering(base, counters):
    @event.listens_for(base, "before_insert", propagate=True)
    def assign_numbers(mapper, connection, record):
        table = mapper.local_table.name
        if table not in ORDER_PREFIXES and table not in VOUCHER_PREFIXES and table not in {"financial_vouchers", "vouchers", "international_purchases", "support_tickets"}:
            return
        extra = dict(record.extra_data or {})
        if table in ORDER_PREFIXES:
            number = next_document_number(connection, counters, ORDER_PREFIXES[table])
            if "order_number" in mapper.local_table.c:
                record.order_number = number
                extra.pop("order_number", None)
            else:
                extra["order_number"] = number
            extra["invoice_number"] = next_document_number(connection, counters, "INV")
        elif table in {"financial_vouchers", "vouchers"}:
            prefix = "PV" if str(extra.get("voucher_type") or record.type or "").lower() == "payment" else "RV"
            extra["voucher_number"] = next_document_number(connection, counters, prefix)
        elif table in VOUCHER_PREFIXES:
            extra["voucher_number"] = next_document_number(connection, counters, VOUCHER_PREFIXES[table])
        elif table == "international_purchases":
            extra["purchase_number"] = next_document_number(connection, counters, "PUR")
        else:
            extra["ticket_number"] = next_document_number(connection, counters, "SUP")
        record.extra_data = extra
