from __future__ import annotations

from alembic import op


revision = "20260730_01"
down_revision = "20260724_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_email_lower_lookup
            ON users ((lower(email)));
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_login_attempts_email_ip_success_created
            ON login_attempts (email, ip_address, succeeded, created_at DESC);
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_login_attempts_detail_created_email_ip
            ON login_attempts (detail, created_at DESC, email, ip_address);
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_refresh_tokens_user_revoked_expires
            ON refresh_tokens (user_id, revoked_at, expires_at);
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_refresh_tokens_user_revoked_expires")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_login_attempts_detail_created_email_ip")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_login_attempts_email_ip_success_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_users_email_lower_lookup")
