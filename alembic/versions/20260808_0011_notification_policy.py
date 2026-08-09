"""Add explicit notification thresholds per destination.

Revision ID: 20260808_0011
Revises: 20260808_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0011"
down_revision: str | None = "20260808_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.add_column(
            sa.Column("manager_notification_threshold", sa.Integer(), nullable=False, server_default="65")
        )
        batch.add_column(
            sa.Column("employee_notification_threshold", sa.Integer(), nullable=False, server_default="70")
        )
        batch.add_column(
            sa.Column("group_notification_threshold", sa.Integer(), nullable=False, server_default="85")
        )
        batch.add_column(
            sa.Column("notification_immediate_threshold", sa.Integer(), nullable=False, server_default="90")
        )
        batch.create_check_constraint(
            "ck_tenant_settings_notification_thresholds",
            "manager_notification_threshold BETWEEN 0 AND 100 AND "
            "employee_notification_threshold BETWEEN 0 AND 100 AND "
            "group_notification_threshold BETWEEN 0 AND 100 AND "
            "notification_immediate_threshold BETWEEN 0 AND 100",
        )
    op.execute(
        "UPDATE tenant_settings SET "
        "manager_notification_threshold = signal_problem_threshold, "
        "employee_notification_threshold = signal_problem_threshold, "
        "group_notification_threshold = signal_immediate_threshold, "
        "notification_immediate_threshold = signal_immediate_threshold"
    )


def downgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.drop_constraint("ck_tenant_settings_notification_thresholds", type_="check")
        batch.drop_column("notification_immediate_threshold")
        batch.drop_column("group_notification_threshold")
        batch.drop_column("employee_notification_threshold")
        batch.drop_column("manager_notification_threshold")
