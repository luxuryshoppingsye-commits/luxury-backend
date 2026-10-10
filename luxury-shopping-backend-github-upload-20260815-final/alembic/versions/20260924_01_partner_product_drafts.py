"""Store one private product draft per partner account."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "20260924_01"
down_revision = "20260920_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("partner_product_drafts"):
        columns = {column["name"]: column for column in inspector.get_columns("partner_product_drafts")}
        expected = {"partner_id": UUID, "payload": JSONB, "created_at": sa.DateTime, "updated_at": sa.DateTime}
        for name, column_type in expected.items():
            column = columns.get(name)
            if column is None or not isinstance(column["type"], column_type) or column["nullable"]:
                raise RuntimeError(f"Existing partner_product_drafts.{name} has an incompatible definition")
            if name.endswith("_at") and not column["type"].timezone:
                raise RuntimeError(f"Existing partner_product_drafts.{name} must retain timezone information")
        if inspector.get_pk_constraint("partner_product_drafts")["constrained_columns"] != ["partner_id"]:
            raise RuntimeError("Existing partner_product_drafts must have partner_id as its primary key")
        if not any(
            key["constrained_columns"] == ["partner_id"]
            and key["referred_table"] == "users"
            and key["referred_columns"] == ["id"]
            and str(key.get("options", {}).get("ondelete", "")).upper() == "CASCADE"
            for key in inspector.get_foreign_keys("partner_product_drafts")
        ):
            raise RuntimeError("Existing partner_product_drafts must retain its cascading partner foreign key")
        return
    op.create_table(
        "partner_product_drafts",
        sa.Column("partner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("partner_product_drafts")
