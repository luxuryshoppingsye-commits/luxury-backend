from __future__ import annotations

from alembic import op


revision = "20260724_05"
down_revision = "20260724_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_backup_records_status_official'
            ) THEN
                ALTER TABLE backup_records
                ADD CONSTRAINT ck_backup_records_status_official
                CHECK (
                    status IN (
                        'requested','acquiring_lock','dumping_database','collecting_files',
                        'building_manifest','encrypting','verifying_local_bundle',
                        'uploading_offsite','verifying_offsite_copy','restoring_test_database',
                        'verifying_restored_files','ready','failed','cancelled','expired','deleting'
                    )
                ) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_backup_records_ready_requires_verified_bundle'
            ) THEN
                ALTER TABLE backup_records
                ADD CONSTRAINT ck_backup_records_ready_requires_verified_bundle
                CHECK (
                    status <> 'ready'
                    OR (
                        path IS NOT NULL
                        AND length(trim(path)) > 0
                        AND extra_data ? 'encrypted_bundle_key'
                        AND extra_data ? 'encrypted_checksum'
                        AND extra_data ? 'offsite_status'
                        AND extra_data->>'offsite_status' = 'verified'
                        AND extra_data ? 'verification_status'
                        AND extra_data->>'verification_status' = 'verified'
                        AND extra_data ? 'restore_verification_status'
                        AND extra_data->>'restore_verification_status' IN ('verified','not_required')
                        AND coalesce((extra_data->>'size_bytes')::bigint, 0) > 0
                    )
                ) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_sync_events_realtime_payload_shape'
            ) THEN
                ALTER TABLE sync_events
                ADD CONSTRAINT ck_sync_events_realtime_payload_shape
                CHECK (
                    type NOT LIKE 'realtime_event:%'
                    OR (
                        extra_data ? 'event_id'
                        AND extra_data ? 'event'
                        AND extra_data ? 'channel'
                        AND extra_data ? 'payload'
                    )
                ) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_sync_events_realtime_ticket_shape'
            ) THEN
                ALTER TABLE sync_events
                ADD CONSTRAINT ck_sync_events_realtime_ticket_shape
                CHECK (
                    type <> 'realtime_ticket'
                    OR (
                        description IS NOT NULL
                        AND length(description) = 64
                        AND extra_data ? 'channels'
                        AND extra_data ? 'expires_at'
                        AND extra_data ? 'single_use'
                    )
                ) NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_backup_records_single_active_job
        ON backup_records ((coalesce(extra_data->>'lock_key', 'official')))
        WHERE deleted_at IS NULL
          AND status IN (
            'requested','acquiring_lock','dumping_database','collecting_files',
            'building_manifest','encrypting','verifying_local_bundle',
            'uploading_offsite','verifying_offsite_copy','restoring_test_database',
            'verifying_restored_files','deleting'
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_backup_records_status_created
        ON backup_records (status, created_at)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_events_realtime_ticket_hash_active
        ON sync_events (description)
        WHERE deleted_at IS NULL AND type = 'realtime_ticket'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_events_realtime_event_dedupe_active
        ON sync_events (type, description)
        WHERE deleted_at IS NULL AND type LIKE 'realtime_event:%'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_sync_events_realtime_event_channel_created
        ON sync_events ((extra_data->>'channel'), created_at)
        WHERE deleted_at IS NULL AND type LIKE 'realtime_event:%'
        """
    )
    op.execute(
        """
        -- PostgreSQL does not allow a timestamptz cast in an index expression
        -- because the cast depends on the session timezone and is not IMMUTABLE.
        -- Keep the immutable text extraction index; expiry is validated by the
        -- realtime service after parsing the ISO-8601 value.
        CREATE INDEX IF NOT EXISTS ix_sync_events_realtime_ticket_expiry
        ON sync_events ((extra_data->>'expires_at'))
        WHERE deleted_at IS NULL AND type = 'realtime_ticket'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sync_events_realtime_ticket_expiry")
    op.execute("DROP INDEX IF EXISTS ix_sync_events_realtime_event_channel_created")
    op.execute("DROP INDEX IF EXISTS ux_sync_events_realtime_event_dedupe_active")
    op.execute("DROP INDEX IF EXISTS ux_sync_events_realtime_ticket_hash_active")
    op.execute("DROP INDEX IF EXISTS ix_backup_records_status_created")
    op.execute("DROP INDEX IF EXISTS ux_backup_records_single_active_job")
    op.execute("ALTER TABLE sync_events DROP CONSTRAINT IF EXISTS ck_sync_events_realtime_ticket_shape")
    op.execute("ALTER TABLE sync_events DROP CONSTRAINT IF EXISTS ck_sync_events_realtime_payload_shape")
    op.execute("ALTER TABLE backup_records DROP CONSTRAINT IF EXISTS ck_backup_records_ready_requires_verified_bundle")
    op.execute("ALTER TABLE backup_records DROP CONSTRAINT IF EXISTS ck_backup_records_status_official")
