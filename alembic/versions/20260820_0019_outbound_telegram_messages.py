"""Add idempotent outbound Telegram commands.

Revision ID: 20260820_0019
Revises: 20260814_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260820_0019"
down_revision: str | None = "20260814_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_telegram_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("problem_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("dialog_id", sa.String(36), nullable=False),
        sa.Column("client_request_id", sa.String(36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("telegram_random_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["problem_id"], ["operational_problems.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["telegram_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["dialog_id"], ["telegram_dialogs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "client_request_id", name="uq_outbound_telegram_client_request"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbound_telegram_attempts"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed')",
            name="ck_outbound_telegram_status",
        ),
    )
    for column in (
        "tenant_id",
        "problem_id",
        "connection_id",
        "dialog_id",
        "status",
        "telegram_message_id",
        "sent_at",
    ):
        op.create_index(
            f"ix_outbound_telegram_messages_{column}",
            "outbound_telegram_messages",
            [column],
        )


def downgrade() -> None:
    op.drop_table("outbound_telegram_messages")
