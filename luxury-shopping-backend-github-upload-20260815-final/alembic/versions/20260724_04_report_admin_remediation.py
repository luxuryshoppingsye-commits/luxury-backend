from __future__ import annotations

from alembic import op


revision = "20260724_04"
down_revision = "20260724_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_report_exports_status_official'
            ) THEN
                ALTER TABLE report_exports
                ADD CONSTRAINT ck_report_exports_status_official
                CHECK (status IN ('requested','queued','generating','ready','failed','expired','cancelled')) NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_report_exports_ready_file'
            ) THEN
                ALTER TABLE report_exports
                ADD CONSTRAINT ck_report_exports_ready_file
                CHECK (
                    status <> 'ready'
                    OR (
                        path IS NOT NULL
                        AND length(trim(path)) > 0
                        AND extra_data ? 'file_id'
                        AND extra_data ? 'storage_key'
                        AND coalesce((extra_data->>'size_bytes')::bigint, 0) > 0
                    )
                ) NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_courier_location_latitude_range'
            ) THEN
                ALTER TABLE courier_location_updates
                ADD CONSTRAINT ck_courier_location_latitude_range
                CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_courier_location_longitude_range'
            ) THEN
                ALTER TABLE courier_location_updates
                ADD CONSTRAINT ck_courier_location_longitude_range
                CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)) NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_operational_days_date_active
        ON operational_days ((extra_data->>'date'))
        WHERE deleted_at IS NULL AND extra_data ? 'date'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_cursor_user_stream_device
        ON sync_events (user_id, type, description)
        WHERE deleted_at IS NULL AND type LIKE 'sync_cursor:%'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_campaign_delivery_dedupe_active
        ON analytics_events ((extra_data->>'dedupe_key'))
        WHERE deleted_at IS NULL
            AND type = 'campaign_delivery'
            AND extra_data ? 'dedupe_key'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_report_exports_status_created
        ON report_exports (status, created_at)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_courier_location_assignment_created
        ON courier_location_updates (assignment_id, created_at)
        WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_courier_location_assignment_created")
    op.execute("DROP INDEX IF EXISTS ix_report_exports_status_created")
    op.execute("DROP INDEX IF EXISTS ux_campaign_delivery_dedupe_active")
    op.execute("DROP INDEX IF EXISTS ux_sync_cursor_user_stream_device")
    op.execute("DROP INDEX IF EXISTS ux_operational_days_date_active")
    op.execute("ALTER TABLE courier_location_updates DROP CONSTRAINT IF EXISTS ck_courier_location_longitude_range")
    op.execute("ALTER TABLE courier_location_updates DROP CONSTRAINT IF EXISTS ck_courier_location_latitude_range")
    op.execute("ALTER TABLE report_exports DROP CONSTRAINT IF EXISTS ck_report_exports_ready_file")
    op.execute("ALTER TABLE report_exports DROP CONSTRAINT IF EXISTS ck_report_exports_status_official")
