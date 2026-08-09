"""Add Telegram runtime fencing, dialog timers and durable partitions.

Revision ID: 20260809_0014
Revises: 20260809_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0014"
down_revision: str | None = "20260809_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_connections") as batch:
        batch.add_column(
            sa.Column("edited_updates_received", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_reconnect_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_catchup_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("rate_limited_until", sa.DateTime(timezone=True)))
    op.create_table(
        "telegram_runtime_leases",
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_instance_id", sa.String(200), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_telegram_runtime_leases_owner_instance_id",
        "telegram_runtime_leases",
        ["owner_instance_id"],
    )
    op.create_index(
        "ix_telegram_runtime_leases_lease_until", "telegram_runtime_leases", ["lease_until"]
    )
    with op.batch_alter_table("dialog_states") as batch:
        batch.add_column(sa.Column("awaiting_employee_since", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("awaiting_customer_since", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column(
                "response_expected_message_id",
                sa.String(36),
                sa.ForeignKey(
                    "telegram_messages.id",
                    name="fk_dialog_states_response_expected_message",
                    ondelete="SET NULL",
                ),
            )
        )
        batch.add_column(sa.Column("next_sla_check_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_employee_reply_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_customer_message_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_dialog_states_next_sla_check_at", ["next_sla_check_at"])
    with op.batch_alter_table("operational_problems") as batch:
        batch.add_column(sa.Column("next_check_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_operational_problems_next_check_at", ["next_check_at"])
    with op.batch_alter_table("background_jobs") as batch:
        batch.add_column(sa.Column("partition_key", sa.String(200)))
        batch.add_column(sa.Column("partition_sequence", sa.BigInteger()))
        batch.create_index("ix_background_jobs_partition_key", ["partition_key"])
        batch.create_index("ix_background_jobs_partition_sequence", ["partition_sequence"])
        batch.create_index(
            "ix_background_jobs_partition_order", ["partition_key", "partition_sequence", "status"]
        )
    with op.batch_alter_table("owner_client_drafts") as batch:
        batch.add_column(
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "prompt_version",
                sa.String(32),
                nullable=False,
                server_default="client-draft-v1",
            )
        )
        batch.add_column(sa.Column("parse_latency_ms", sa.Integer()))
        batch.add_column(
            sa.Column("manual_changes_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("confirmation_actor_id", sa.BigInteger()))


def downgrade() -> None:
    with op.batch_alter_table("owner_client_drafts") as batch:
        for name in (
            "confirmation_actor_id",
            "manual_changes_json",
            "parse_latency_ms",
            "prompt_version",
            "schema_version",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("background_jobs") as batch:
        batch.drop_index("ix_background_jobs_partition_order")
        batch.drop_index("ix_background_jobs_partition_sequence")
        batch.drop_index("ix_background_jobs_partition_key")
        batch.drop_column("partition_sequence")
        batch.drop_column("partition_key")
    with op.batch_alter_table("operational_problems") as batch:
        batch.drop_index("ix_operational_problems_next_check_at")
        batch.drop_column("next_check_at")
    with op.batch_alter_table("dialog_states") as batch:
        batch.drop_index("ix_dialog_states_next_sla_check_at")
        for name in (
            "last_customer_message_at",
            "last_employee_reply_at",
            "next_sla_check_at",
            "response_expected_message_id",
            "awaiting_customer_since",
            "awaiting_employee_since",
        ):
            batch.drop_column(name)
    op.drop_table("telegram_runtime_leases")
    with op.batch_alter_table("telegram_connections") as batch:
        for name in (
            "rate_limited_until",
            "last_catchup_at",
            "last_reconnect_at",
            "edited_updates_received",
        ):
            batch.drop_column(name)
