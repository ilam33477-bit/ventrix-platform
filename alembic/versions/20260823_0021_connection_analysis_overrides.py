"""Add per-connection analysis overrides.

Revision ID: 20260823_0021
Revises: 20260821_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0021"
down_revision: str | None = "20260821_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_connections") as batch:
        batch.add_column(sa.Column("response_sla_minutes_override", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("signal_problem_threshold_override", sa.Integer(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_telegram_connections_sla_override",
            "response_sla_minutes_override IS NULL OR "
            "response_sla_minutes_override BETWEEN 5 AND 1440",
        )
        batch.create_check_constraint(
            "ck_telegram_connections_problem_threshold_override",
            "signal_problem_threshold_override IS NULL OR "
            "signal_problem_threshold_override BETWEEN 0 AND 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("telegram_connections") as batch:
        batch.drop_constraint("ck_telegram_connections_problem_threshold_override", type_="check")
        batch.drop_constraint("ck_telegram_connections_sla_override", type_="check")
        batch.drop_column("signal_problem_threshold_override")
        batch.drop_column("response_sla_minutes_override")
