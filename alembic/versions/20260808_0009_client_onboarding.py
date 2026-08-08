"""Persist client Mini App onboarding state per tenant.

Revision ID: 20260808_0009
Revises: 20260808_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0009"
down_revision: str | None = "20260808_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.add_column(
            sa.Column(
                "client_onboarding_step",
                sa.String(length=32),
                nullable=False,
                server_default="welcome",
            )
        )
        batch.add_column(
            sa.Column("client_onboarding_completed_at", sa.DateTime(timezone=True), nullable=True)
        )

    # Existing clients that already completed a Telegram connection must open
    # directly on the dashboard after this migration.
    op.execute(
        sa.text(
            """
            UPDATE tenant_settings
            SET client_onboarding_step = 'completed',
                client_onboarding_completed_at = CURRENT_TIMESTAMP
            WHERE EXISTS (
                SELECT 1
                FROM telegram_connections
                WHERE telegram_connections.tenant_id = tenant_settings.tenant_id
                  AND telegram_connections.deleted_at IS NULL
                  AND telegram_connections.status IN ('connected', 'syncing', 'ready')
            )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.drop_column("client_onboarding_completed_at")
        batch.drop_column("client_onboarding_step")
