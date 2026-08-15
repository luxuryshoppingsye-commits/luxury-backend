from __future__ import annotations

from alembic import op


revision = "20260724_03"
down_revision = "20260724_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_email_outbox_dedupe_active
        ON email_outbox ((extra_data->>'dedupe_key'))
        WHERE extra_data ? 'dedupe_key' AND deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_whatsapp_outbox_dedupe_active
        ON whatsapp_outbox ((extra_data->>'dedupe_key'))
        WHERE extra_data ? 'dedupe_key' AND deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_outbox_dedupe_active
        ON notification_outbox ((extra_data->>'dedupe_key'))
        WHERE extra_data ? 'dedupe_key' AND deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_notifications_recipient_dedupe_active
        ON admin_notifications (recipient_id, deduplication_key)
        WHERE recipient_id IS NOT NULL AND deduplication_key IS NOT NULL AND deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_email_outbox_status_created
        ON email_outbox (status, created_at)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_whatsapp_outbox_status_created
        ON whatsapp_outbox (status, created_at)
        WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_outbox_status_created")
    op.execute("DROP INDEX IF EXISTS ix_email_outbox_status_created")
    op.execute("DROP INDEX IF EXISTS ux_admin_notifications_recipient_dedupe_active")
    op.execute("DROP INDEX IF EXISTS ux_notification_outbox_dedupe_active")
    op.execute("DROP INDEX IF EXISTS ux_whatsapp_outbox_dedupe_active")
    op.execute("DROP INDEX IF EXISTS ux_email_outbox_dedupe_active")
