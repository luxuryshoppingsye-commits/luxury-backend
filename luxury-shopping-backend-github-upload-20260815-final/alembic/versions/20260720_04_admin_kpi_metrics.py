from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260720_04"
down_revision = "20260720_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    risk_alert_columns = {column["name"] for column in inspector.get_columns("risk_alerts")}
    index_names = {index["name"] for index in inspector.get_indexes("risk_alerts")}
    if "is_acknowledged" not in risk_alert_columns:
        op.add_column(
            "risk_alerts",
            sa.Column(
                "is_acknowledged",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=True,
            ),
        )
    if "ix_risk_alerts_is_acknowledged" not in index_names:
        op.create_index(
            "ix_risk_alerts_is_acknowledged",
            "risk_alerts",
            ["is_acknowledged"],
            unique=False,
        )
    if "kpi_metrics" not in inspector.get_table_names():
        op.create_table(
            "kpi_metrics",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("name", sa.String(length=500), nullable=True),
            sa.Column("type", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=True),
            sa.Column("value", sa.Numeric(18, 2), server_default="0", nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), server_default="0", nullable=True),
            sa.Column("extra_data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    kpi_index_names = {index["name"] for index in inspector.get_indexes("kpi_metrics")}
    if "ix_kpi_metrics_name" not in kpi_index_names:
        op.create_index("ix_kpi_metrics_name", "kpi_metrics", ["name"], unique=False)
    if "ix_kpi_metrics_type" not in kpi_index_names:
        op.create_index("ix_kpi_metrics_type", "kpi_metrics", ["type"], unique=False)
    if "ix_kpi_metrics_status" not in kpi_index_names:
        op.create_index("ix_kpi_metrics_status", "kpi_metrics", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_kpi_metrics_status", table_name="kpi_metrics")
    op.drop_index("ix_kpi_metrics_type", table_name="kpi_metrics")
    op.drop_index("ix_kpi_metrics_name", table_name="kpi_metrics")
    op.drop_table("kpi_metrics")
    op.drop_index("ix_risk_alerts_is_acknowledged", table_name="risk_alerts")
    op.drop_column("risk_alerts", "is_acknowledged")
