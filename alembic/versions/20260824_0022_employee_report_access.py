"""Add employee report access and project bot activity.

Revision ID: 20260824_0022
Revises: 20260823_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0022"
down_revision: str | None = "20260823_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.add_column(
            sa.Column("reports_access_all", sa.Boolean(), server_default="0", nullable=False)
        )
    with op.batch_alter_table("tenant_memberships") as batch:
        batch.add_column(sa.Column("bot_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenant_memberships") as batch:
        batch.drop_column("bot_started_at")
    with op.batch_alter_table("employees") as batch:
        batch.drop_column("reports_access_all")
