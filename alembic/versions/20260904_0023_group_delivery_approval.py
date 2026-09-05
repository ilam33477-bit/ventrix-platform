"""Require explicit project authorization for group delivery.

Legacy active rows have no approval provenance and must be reconnected by a
project manager. Do not infer authorization from the bot's Telegram admin role.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260904_0023"
down_revision = "20260824_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_integrations", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column("group_integrations", sa.Column("approved_by_telegram_user_id", sa.BigInteger()))
    op.execute("UPDATE group_integrations SET status = 'pending' WHERE status = 'active'")


def downgrade() -> None:
    op.drop_column("group_integrations", "approved_by_telegram_user_id")
    op.drop_column("group_integrations", "approved_at")
