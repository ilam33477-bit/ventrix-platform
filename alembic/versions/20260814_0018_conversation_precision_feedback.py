"""Add canonical issue identity and tenant AI feedback profile.

Revision ID: 20260814_0018
Revises: 20260809_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260814_0018"
down_revision: str | None = "20260809_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operational_problems") as batch:
        batch.add_column(sa.Column("issue_family", sa.String(64), nullable=True))
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "evidence_message_ids_json",
                sa.JSON(),
                server_default="[]",
                nullable=False,
            )
        )
        batch.create_index("ix_operational_problems_issue_family", ["issue_family"])
        batch.create_index("ix_operational_problems_last_seen_at", ["last_seen_at"])
        batch.create_index(
            "ix_operational_problems_tenant_dialog_family_status",
            ["tenant_id", "dialog_id", "issue_family", "status"],
        )
    op.execute(
        """
        UPDATE operational_problems
        SET issue_family = CASE
          WHEN problem_type IN ('client_without_answer','customer_question','unanswered_customer')
            THEN 'UNANSWERED_REQUEST'
          WHEN problem_type IN ('technical_problem','support_problem') THEN 'TECHNICAL_PROBLEM'
          WHEN problem_type IN ('payment_question','invoice_received') THEN 'PAYMENT_QUESTION'
          WHEN problem_type IN ('customer_complaint','complaint','product_dissatisfaction')
            THEN 'PRODUCT_DISSATISFACTION'
          WHEN problem_type IN ('employee_commitment','commitment_risk','promise_deadline')
            THEN 'PROMISE_DEADLINE'
          WHEN problem_type IN ('new_lead','lead','commercial_opportunity','partnership_opportunity')
            THEN 'COMMERCIAL_OPPORTUNITY'
          ELSE 'OTHER'
        END,
        last_seen_at = occurred_at,
        evidence_message_ids_json = json_array(source_message_id)
        WHERE issue_family IS NULL
        """
    )

    op.create_table(
        "tenant_ai_feedback_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("guidance_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("source_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_processed_transition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synthesized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id"),
        sa.CheckConstraint("source_count >= 0", name="ck_tenant_ai_feedback_source_count"),
        sa.CheckConstraint("version >= 0", name="ck_tenant_ai_feedback_version"),
    )
    op.create_index(
        "ix_tenant_ai_feedback_profiles_tenant_id",
        "tenant_ai_feedback_profiles",
        ["tenant_id"],
        unique=True,
    )
    op.create_index(
        "ix_tenant_ai_feedback_profiles_last_processed_transition_at",
        "tenant_ai_feedback_profiles",
        ["last_processed_transition_at"],
    )
    op.create_index(
        "ix_tenant_ai_feedback_profiles_last_synthesized_at",
        "tenant_ai_feedback_profiles",
        ["last_synthesized_at"],
    )


def downgrade() -> None:
    op.drop_table("tenant_ai_feedback_profiles")
    with op.batch_alter_table("operational_problems") as batch:
        batch.drop_index("ix_operational_problems_tenant_dialog_family_status")
        batch.drop_index("ix_operational_problems_last_seen_at")
        batch.drop_index("ix_operational_problems_issue_family")
        batch.drop_column("evidence_message_ids_json")
        batch.drop_column("last_seen_at")
        batch.drop_column("issue_family")
