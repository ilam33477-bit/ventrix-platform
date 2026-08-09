"""Persist the operational problem lifecycle and remediation evidence.

Revision ID: 20260808_0010
Revises: 20260808_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0010"
down_revision: str | None = "20260808_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operational_problems") as batch:
        batch.add_column(sa.Column("responsible_employee_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("closed_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resolution_evidence", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_operational_problems_responsible_employee",
            "employees",
            ["responsible_employee_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_operational_problems_responsible_employee_id",
            ["responsible_employee_id"],
        )
        batch.create_index("ix_operational_problems_deadline_at", ["deadline_at"])

    # `open` was the legacy catch-all state. Preserve every row while moving it
    # into the explicit FSM entry state.
    op.execute("UPDATE operational_problems SET status = 'new' WHERE status = 'open'")

    op.create_table(
        "problem_transitions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("problem_id", sa.String(36), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["problem_id"], ["operational_problems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_problem_transitions_tenant_id", "problem_transitions", ["tenant_id"])
    op.create_index("ix_problem_transitions_problem_id", "problem_transitions", ["problem_id"])
    op.create_index("ix_problem_transitions_to_status", "problem_transitions", ["to_status"])
    op.create_index("ix_problem_transitions_actor_id", "problem_transitions", ["actor_id"])
    op.create_index("ix_problem_transitions_occurred_at", "problem_transitions", ["occurred_at"])

    op.create_table(
        "problem_verifications",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("problem_id", sa.String(36), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids_json", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_problem_verification_confidence"),
        sa.ForeignKeyConstraint(["problem_id"], ["operational_problems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_problem_verifications_tenant_id", "problem_verifications", ["tenant_id"])
    op.create_index("ix_problem_verifications_problem_id", "problem_verifications", ["problem_id"])
    op.create_index("ix_problem_verifications_outcome", "problem_verifications", ["outcome"])
    op.create_index("ix_problem_verifications_checked_at", "problem_verifications", ["checked_at"])


def downgrade() -> None:
    op.drop_table("problem_verifications")
    op.drop_table("problem_transitions")
    op.execute("UPDATE operational_problems SET status = 'open' WHERE status = 'new'")
    with op.batch_alter_table("operational_problems") as batch:
        batch.drop_index("ix_operational_problems_deadline_at")
        batch.drop_index("ix_operational_problems_responsible_employee_id")
        batch.drop_constraint("fk_operational_problems_responsible_employee", type_="foreignkey")
        batch.drop_column("resolution_evidence")
        batch.drop_column("closed_reason")
        batch.drop_column("last_verified_at")
        batch.drop_column("reopened_at")
        batch.drop_column("deadline_at")
        batch.drop_column("responsible_employee_id")
