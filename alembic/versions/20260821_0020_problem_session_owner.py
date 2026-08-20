"""Backfill problem responsibility from the originating Telegram session.

Revision ID: 20260821_0020
Revises: 20260820_0019
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_0020"
down_revision: str | None = "20260820_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE operational_problems
        SET responsible_employee_id = (
            SELECT telegram_connections.assigned_employee_id
            FROM telegram_connections
            WHERE telegram_connections.id = operational_problems.connection_id
              AND telegram_connections.tenant_id = operational_problems.tenant_id
              AND telegram_connections.assigned_employee_id IS NOT NULL
        )
        WHERE responsible_employee_id IS NULL
          AND EXISTS (
              SELECT 1
              FROM telegram_connections
              WHERE telegram_connections.id = operational_problems.connection_id
                AND telegram_connections.tenant_id = operational_problems.tenant_id
                AND telegram_connections.assigned_employee_id IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    # The originating session remains the source of truth; a destructive
    # responsibility rollback would erase valid production assignments.
    pass
