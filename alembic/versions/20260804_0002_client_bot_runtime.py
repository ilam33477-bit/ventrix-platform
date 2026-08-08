"""Client bot runtime state and product events.

Revision ID: 20260804_0002
Revises: 20260804_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bot_instances") as batch:
        batch.add_column(sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False))
        batch.add_column(
            sa.Column("runtime_status", sa.String(32), server_default="stopped", nullable=False)
        )
        batch.add_column(sa.Column("last_started_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_update_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("runtime_heartbeat_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("processed_updates", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("button_clicks", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("runtime_restart_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("runtime_generation", sa.Integer(), server_default="1", nullable=False)
        )
        batch.create_check_constraint(
            "ck_bot_instances_runtime_status",
            "runtime_status IN ('stopped','starting','running','failed','stopping')",
        )
        batch.create_check_constraint(
            "ck_bot_instances_runtime_counters",
            "processed_updates >= 0 AND button_clicks >= 0 AND runtime_restart_count >= 0 "
            "AND runtime_generation > 0",
        )
        batch.create_index("ix_bot_instances_enabled", ["enabled"])
        batch.create_index("ix_bot_instances_runtime_status", ["runtime_status"])
        batch.create_index("ix_bot_instances_runtime_heartbeat_at", ["runtime_heartbeat_at"])

    op.create_table(
        "product_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bot_instance_id",
            sa.String(36),
            sa.ForeignKey("bot_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("event_name", sa.String(100), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    for name, columns in (
        ("ix_product_events_tenant_id", ["tenant_id"]),
        ("ix_product_events_bot_instance_id", ["bot_instance_id"]),
        ("ix_product_events_telegram_user_id", ["telegram_user_id"]),
        ("ix_product_events_event_name", ["event_name"]),
        ("ix_product_events_occurred_at", ["occurred_at"]),
        ("ix_product_events_bot_occurred", ["bot_instance_id", "occurred_at"]),
        ("ix_product_events_tenant_event", ["tenant_id", "event_name"]),
    ):
        op.create_index(name, "product_events", columns)


def downgrade() -> None:
    op.drop_table("product_events")
    with op.batch_alter_table("bot_instances") as batch:
        batch.drop_index("ix_bot_instances_runtime_heartbeat_at")
        batch.drop_index("ix_bot_instances_runtime_status")
        batch.drop_index("ix_bot_instances_enabled")
        batch.drop_constraint("ck_bot_instances_runtime_counters", type_="check")
        batch.drop_constraint("ck_bot_instances_runtime_status", type_="check")
        for column in (
            "runtime_generation",
            "runtime_restart_count",
            "button_clicks",
            "processed_updates",
            "runtime_heartbeat_at",
            "last_update_at",
            "last_started_at",
            "runtime_status",
            "enabled",
        ):
            batch.drop_column(column)
