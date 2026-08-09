"""Add owner-controlled Telegram history windows.

Revision ID: 20260809_0016
Revises: 20260809_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0016"
down_revision: str | None = "20260809_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.add_column(
            sa.Column("active_dialog_days", sa.Integer(), server_default="30", nullable=False)
        )
        batch.add_column(
            sa.Column("message_history_days", sa.Integer(), server_default="14", nullable=False)
        )
        batch.create_check_constraint(
            "ck_tenant_settings_active_dialog_days", "active_dialog_days BETWEEN 0 AND 180"
        )
        batch.create_check_constraint(
            "ck_tenant_settings_message_history_days",
            "message_history_days BETWEEN 0 AND 180",
        )
    with op.batch_alter_table("telegram_connections") as batch:
        batch.drop_constraint("ck_telegram_connections_history_days", type_="check")
        batch.create_check_constraint(
            "ck_telegram_connections_history_days", "history_days BETWEEN 0 AND 180"
        )
    # The new order is deliberately shorter and human-facing. Incomplete legacy
    # sessions restart safely; completed tenants remain completed.
    op.execute(
        "UPDATE tenant_settings SET client_onboarding_step = 'welcome', "
        "client_onboarding_json = '{}' WHERE client_onboarding_completed_at IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE telegram_connections SET history_days = 7 WHERE history_days NOT IN (3, 7, 14, 30)"
    )
    with op.batch_alter_table("telegram_connections") as batch:
        batch.drop_constraint("ck_telegram_connections_history_days", type_="check")
        batch.create_check_constraint(
            "ck_telegram_connections_history_days", "history_days IN (3,7,14,30)"
        )
    with op.batch_alter_table("tenant_settings") as batch:
        batch.drop_constraint("ck_tenant_settings_message_history_days", type_="check")
        batch.drop_constraint("ck_tenant_settings_active_dialog_days", type_="check")
        batch.drop_column("message_history_days")
        batch.drop_column("active_dialog_days")
