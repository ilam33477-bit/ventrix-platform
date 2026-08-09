"""Make scheduled AI batches token-budget and multi-dialog aware.

Revision ID: 20260809_0017
Revises: 20260809_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0017"
down_revision: str | None = "20260809_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_batches") as batch:
        batch.drop_constraint("uq_analysis_batch_run_dialog", type_="unique")
        batch.alter_column("dialog_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("batch_key", sa.String(100), nullable=True))
        batch.add_column(
            sa.Column("estimated_input_tokens", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("input_budget", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("prompt_bytes", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("dialogs_count", sa.Integer(), server_default="1", nullable=False)
        )
        batch.add_column(
            sa.Column("messages_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("utilization_ratio", sa.Float(), server_default="0", nullable=False)
        )
    op.execute("UPDATE analysis_batches SET batch_key = id WHERE batch_key IS NULL")
    with op.batch_alter_table("analysis_batches") as batch:
        batch.alter_column("batch_key", existing_type=sa.String(100), nullable=False)
        batch.create_unique_constraint("uq_analysis_batch_run_key", ["run_id", "batch_key"])


def downgrade() -> None:
    # Multi-dialog rows cannot satisfy the legacy non-null dialog invariant.
    op.execute("DELETE FROM analysis_batches WHERE dialog_id IS NULL")
    with op.batch_alter_table("analysis_batches") as batch:
        batch.drop_constraint("uq_analysis_batch_run_key", type_="unique")
        batch.drop_column("utilization_ratio")
        batch.drop_column("messages_count")
        batch.drop_column("dialogs_count")
        batch.drop_column("prompt_bytes")
        batch.drop_column("input_budget")
        batch.drop_column("estimated_input_tokens")
        batch.drop_column("batch_key")
        batch.alter_column("dialog_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint("uq_analysis_batch_run_dialog", ["run_id", "dialog_id"])
