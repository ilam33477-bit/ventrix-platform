"""Incremental ingestion, signals, commitments and notification policy.

Revision ID: 20260808_0007
Revises: 20260804_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0007"
down_revision: str | None = "20260804_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.add_column(
            sa.Column("signal_report_threshold", sa.Integer(), server_default="40", nullable=False)
        )
        batch.add_column(
            sa.Column("signal_problem_threshold", sa.Integer(), server_default="65", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "signal_immediate_threshold", sa.Integer(), server_default="85", nullable=False
            )
        )
        batch.add_column(sa.Column("ai_daily_soft_limit", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("ai_daily_hard_limit", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "employee_notifications_enabled", sa.Boolean(), server_default="1", nullable=False
            )
        )
        batch.add_column(
            sa.Column("group_reminders_enabled", sa.Boolean(), server_default="0", nullable=False)
        )
        batch.create_check_constraint(
            "ck_tenant_settings_signal_thresholds",
            "signal_report_threshold BETWEEN 0 AND 100 AND signal_problem_threshold BETWEEN signal_report_threshold AND 100 AND signal_immediate_threshold BETWEEN signal_problem_threshold AND 100",
        )
        batch.create_check_constraint(
            "ck_tenant_settings_ai_soft_limit",
            "ai_daily_soft_limit IS NULL OR ai_daily_soft_limit > 0",
        )
        batch.create_check_constraint(
            "ck_tenant_settings_ai_hard_limit",
            "ai_daily_hard_limit IS NULL OR ai_daily_hard_limit > 0",
        )

    with op.batch_alter_table("employees") as batch:
        batch.add_column(sa.Column("telegram_user_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("telegram_username", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("role", sa.String(length=64), server_default="employee", nullable=False)
        )
        batch.add_column(
            sa.Column("notifications_enabled", sa.Boolean(), server_default="1", nullable=False)
        )
        batch.add_column(
            sa.Column("criticality_threshold", sa.Integer(), server_default="85", nullable=False)
        )
        batch.add_column(sa.Column("quiet_hours_start", sa.Time(), nullable=True))
        batch.add_column(sa.Column("quiet_hours_end", sa.Time(), nullable=True))
        batch.create_check_constraint(
            "ck_employee_criticality", "criticality_threshold BETWEEN 0 AND 100"
        )
        batch.create_unique_constraint(
            "uq_employee_direct_telegram", ["tenant_id", "telegram_user_id"]
        )
        batch.create_index("ix_employees_telegram_user_id", ["telegram_user_id"])
        batch.create_index("ix_employees_telegram_username", ["telegram_username"])
        batch.create_index("ix_employees_role", ["role"])

    with op.batch_alter_table("telegram_connections") as batch:
        batch.add_column(sa.Column("assigned_employee_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("last_incremental_sync_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("last_full_reconciliation_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("incremental_state_json", sa.JSON(), server_default="{}", nullable=False)
        )
        batch.add_column(sa.Column("error_state", sa.String(length=200), nullable=True))
        batch.create_foreign_key(
            "fk_telegram_connection_employee",
            "employees",
            ["assigned_employee_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_telegram_connections_assigned_employee_id", ["assigned_employee_id"])
        batch.create_index("ix_telegram_connections_last_event_at", ["last_event_at"])

    with op.batch_alter_table("telegram_dialogs") as batch:
        batch.add_column(
            sa.Column("last_message_id", sa.BigInteger(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE telegram_dialogs SET last_message_id = COALESCE((SELECT MAX(telegram_message_id) "
        "FROM telegram_messages WHERE telegram_messages.dialog_id = telegram_dialogs.id), 0)"
    )

    with op.batch_alter_table("background_jobs") as batch:
        batch.add_column(sa.Column("dialog_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("cost_class", sa.String(length=16), server_default="light", nullable=False)
        )
        batch.create_foreign_key(
            "fk_background_job_dialog",
            "telegram_dialogs",
            ["dialog_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_background_jobs_dialog_id", ["dialog_id"])
        batch.create_index("ix_background_jobs_cost_class", ["cost_class"])

    op.create_table(
        "telegram_incremental_cursors",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("dialog_id", sa.String(length=36), nullable=False),
        sa.Column("last_message_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="idle", nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["telegram_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dialog_id"], ["telegram_dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "dialog_id", name="uq_incremental_cursor_dialog"),
    )
    op.create_index(
        "ix_telegram_incremental_cursors_tenant_id", "telegram_incremental_cursors", ["tenant_id"]
    )
    op.create_index(
        "ix_telegram_incremental_cursors_connection_id",
        "telegram_incremental_cursors",
        ["connection_id"],
    )
    op.create_index(
        "ix_telegram_incremental_cursors_dialog_id", "telegram_incremental_cursors", ["dialog_id"]
    )
    op.create_index(
        "ix_telegram_incremental_cursors_last_sync_at",
        "telegram_incremental_cursors",
        ["last_sync_at"],
    )
    op.create_index(
        "ix_telegram_incremental_cursors_status", "telegram_incremental_cursors", ["status"]
    )

    op.create_table(
        "signals",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("telegram_connection_id", sa.String(length=36), nullable=False),
        sa.Column("dialog_id", sa.String(length=36), nullable=False),
        sa.Column("source_message_id", sa.String(length=36), nullable=False),
        sa.Column("employee_id", sa.String(length=36), nullable=True),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("local_score", sa.Integer(), nullable=False),
        sa.Column("ai_score", sa.Integer(), nullable=True),
        sa.Column("criticality", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="candidate", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("local_score BETWEEN 0 AND 100", name="ck_signals_local_score"),
        sa.CheckConstraint(
            "ai_score IS NULL OR ai_score BETWEEN 0 AND 100", name="ck_signals_ai_score"
        ),
        sa.CheckConstraint("criticality BETWEEN 0 AND 100", name="ck_signals_criticality"),
        sa.ForeignKeyConstraint(["dialog_id"], ["telegram_dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["telegram_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["telegram_connection_id"], ["telegram_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    for name, columns in (
        ("ix_signals_tenant_id", ["tenant_id"]),
        ("ix_signals_telegram_connection_id", ["telegram_connection_id"]),
        ("ix_signals_dialog_id", ["dialog_id"]),
        ("ix_signals_source_message_id", ["source_message_id"]),
        ("ix_signals_employee_id", ["employee_id"]),
        ("ix_signals_signal_type", ["signal_type"]),
        ("ix_signals_local_score", ["local_score"]),
        ("ix_signals_ai_score", ["ai_score"]),
        ("ix_signals_criticality", ["criticality"]),
        ("ix_signals_status", ["status"]),
        ("ix_signals_detected_at", ["detected_at"]),
        ("ix_signals_tenant_status_detected", ["tenant_id", "status", "detected_at"]),
    ):
        op.create_index(name, "signals", columns)

    op.create_table(
        "dialog_states",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("dialog_id", sa.String(length=36), nullable=False),
        sa.Column(
            "relationship_type", sa.String(length=64), server_default="unknown", nullable=False
        ),
        sa.Column("compact_summary", sa.Text(), server_default="", nullable=False),
        sa.Column("open_commitments_json", sa.JSON(), nullable=False),
        sa.Column("unresolved_questions_json", sa.JSON(), nullable=False),
        sa.Column("last_customer_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_employee_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_ai_processed_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meaningful_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["telegram_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dialog_id"], ["telegram_dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialog_id"),
    )
    for name, columns in (
        ("ix_dialog_states_tenant_id", ["tenant_id"]),
        ("ix_dialog_states_connection_id", ["connection_id"]),
        ("ix_dialog_states_dialog_id", ["dialog_id"]),
        ("ix_dialog_states_last_activity_at", ["last_activity_at"]),
    ):
        op.create_index(name, "dialog_states", columns)

    op.create_table(
        "commitments",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("dialog_id", sa.String(length=36), nullable=False),
        sa.Column("source_message_id", sa.String(length=36), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("responsible_employee_id", sa.String(length=36), nullable=True),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("commitment_type", sa.String(length=64), nullable=False),
        sa.Column("expected_action", sa.Text(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_commitments_confidence"),
        sa.ForeignKeyConstraint(["connection_id"], ["telegram_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dialog_id"], ["telegram_dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["responsible_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["telegram_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    for name, columns in (
        ("ix_commitments_tenant_id", ["tenant_id"]),
        ("ix_commitments_connection_id", ["connection_id"]),
        ("ix_commitments_dialog_id", ["dialog_id"]),
        ("ix_commitments_source_message_id", ["source_message_id"]),
        ("ix_commitments_signal_id", ["signal_id"]),
        ("ix_commitments_responsible_employee_id", ["responsible_employee_id"]),
        ("ix_commitments_commitment_type", ["commitment_type"]),
        ("ix_commitments_deadline_at", ["deadline_at"]),
        ("ix_commitments_status", ["status"]),
        ("ix_commitments_tenant_status_deadline", ["tenant_id", "status", "deadline_at"]),
    ):
        op.create_index(name, "commitments", columns)

    with op.batch_alter_table("operational_problems") as batch:
        batch.add_column(sa.Column("signal_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("commitment_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_operational_problem_signal", "signals", ["signal_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_operational_problem_commitment",
            "commitments",
            ["commitment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_operational_problems_signal_id", ["signal_id"])
        batch.create_index("ix_operational_problems_commitment_id", ["commitment_id"])

    op.create_table(
        "group_integrations",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("bot_instance_id", sa.String(length=36), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("participants_count", sa.Integer(), nullable=True),
        sa.Column("notifications_enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("minimum_criticality", sa.Integer(), server_default="85", nullable=False),
        sa.Column("reminder_cooldown_minutes", sa.Integer(), server_default="120", nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "minimum_criticality BETWEEN 0 AND 100", name="ck_group_minimum_criticality"
        ),
        sa.CheckConstraint("reminder_cooldown_minutes > 0", name="ck_group_reminder_cooldown"),
        sa.ForeignKeyConstraint(["bot_instance_id"], ["bot_instances.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "telegram_chat_id", name="uq_group_integration_chat"),
    )
    for name, columns in (
        ("ix_group_integrations_tenant_id", ["tenant_id"]),
        ("ix_group_integrations_bot_instance_id", ["bot_instance_id"]),
        ("ix_group_integrations_telegram_chat_id", ["telegram_chat_id"]),
        ("ix_group_integrations_status", ["status"]),
    ):
        op.create_index(name, "group_integrations", columns)

    op.create_table(
        "notification_logs",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("problem_id", sa.String(length=36), nullable=True),
        sa.Column("commitment_id", sa.String(length=36), nullable=True),
        sa.Column("employee_id", sa.String(length=36), nullable=True),
        sa.Column("group_integration_id", sa.String(length=36), nullable=True),
        sa.Column("destination_type", sa.String(length=32), nullable=False),
        sa.Column("destination_id", sa.String(length=100), nullable=False),
        sa.Column("deduplication_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("criticality", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("criticality BETWEEN 0 AND 100", name="ck_notification_criticality"),
        sa.ForeignKeyConstraint(["commitment_id"], ["commitments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["group_integration_id"], ["group_integrations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["problem_id"], ["operational_problems.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    for name, columns in (
        ("ix_notification_logs_tenant_id", ["tenant_id"]),
        ("ix_notification_logs_signal_id", ["signal_id"]),
        ("ix_notification_logs_problem_id", ["problem_id"]),
        ("ix_notification_logs_commitment_id", ["commitment_id"]),
        ("ix_notification_logs_employee_id", ["employee_id"]),
        ("ix_notification_logs_group_integration_id", ["group_integration_id"]),
        ("ix_notification_logs_destination_type", ["destination_type"]),
        ("ix_notification_logs_status", ["status"]),
        ("ix_notification_logs_cooldown_until", ["cooldown_until"]),
        ("ix_notification_destination_sent", ["tenant_id", "destination_type", "sent_at"]),
    ):
        op.create_index(name, "notification_logs", columns)

    op.create_table(
        "ai_usage_calls",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["background_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_ai_usage_calls_tenant_id", ["tenant_id"]),
        ("ix_ai_usage_calls_job_id", ["job_id"]),
        ("ix_ai_usage_calls_signal_id", ["signal_id"]),
        ("ix_ai_usage_calls_model", ["model"]),
        ("ix_ai_usage_calls_job_type", ["job_type"]),
        ("ix_ai_usage_calls_status", ["status"]),
        ("ix_ai_usage_calls_occurred_at", ["occurred_at"]),
    ):
        op.create_index(name, "ai_usage_calls", columns)


def downgrade() -> None:
    op.drop_table("ai_usage_calls")
    op.drop_table("notification_logs")
    op.drop_table("group_integrations")
    with op.batch_alter_table("operational_problems") as batch:
        batch.drop_index("ix_operational_problems_commitment_id")
        batch.drop_index("ix_operational_problems_signal_id")
        batch.drop_constraint("fk_operational_problem_commitment", type_="foreignkey")
        batch.drop_constraint("fk_operational_problem_signal", type_="foreignkey")
        batch.drop_column("resolved_at")
        batch.drop_column("commitment_id")
        batch.drop_column("signal_id")
    op.drop_table("commitments")
    op.drop_table("dialog_states")
    op.drop_table("signals")
    op.drop_table("telegram_incremental_cursors")
    with op.batch_alter_table("background_jobs") as batch:
        batch.drop_index("ix_background_jobs_cost_class")
        batch.drop_index("ix_background_jobs_dialog_id")
        batch.drop_constraint("fk_background_job_dialog", type_="foreignkey")
        batch.drop_column("cost_class")
        batch.drop_column("dialog_id")
    with op.batch_alter_table("telegram_dialogs") as batch:
        batch.drop_column("last_sync_at")
        batch.drop_column("last_message_id")
    with op.batch_alter_table("telegram_connections") as batch:
        batch.drop_index("ix_telegram_connections_last_event_at")
        batch.drop_index("ix_telegram_connections_assigned_employee_id")
        batch.drop_constraint("fk_telegram_connection_employee", type_="foreignkey")
        batch.drop_column("error_state")
        batch.drop_column("incremental_state_json")
        batch.drop_column("last_full_reconciliation_at")
        batch.drop_column("last_incremental_sync_at")
        batch.drop_column("last_event_at")
        batch.drop_column("assigned_employee_id")
    with op.batch_alter_table("employees") as batch:
        batch.drop_index("ix_employees_role")
        batch.drop_index("ix_employees_telegram_username")
        batch.drop_index("ix_employees_telegram_user_id")
        batch.drop_constraint("uq_employee_direct_telegram", type_="unique")
        batch.drop_constraint("ck_employee_criticality", type_="check")
        batch.drop_column("quiet_hours_end")
        batch.drop_column("quiet_hours_start")
        batch.drop_column("criticality_threshold")
        batch.drop_column("notifications_enabled")
        batch.drop_column("role")
        batch.drop_column("telegram_username")
        batch.drop_column("telegram_user_id")
    with op.batch_alter_table("tenant_settings") as batch:
        batch.drop_constraint("ck_tenant_settings_ai_hard_limit", type_="check")
        batch.drop_constraint("ck_tenant_settings_ai_soft_limit", type_="check")
        batch.drop_constraint("ck_tenant_settings_signal_thresholds", type_="check")
        batch.drop_column("group_reminders_enabled")
        batch.drop_column("employee_notifications_enabled")
        batch.drop_column("ai_daily_hard_limit")
        batch.drop_column("ai_daily_soft_limit")
        batch.drop_column("signal_immediate_threshold")
        batch.drop_column("signal_problem_threshold")
        batch.drop_column("signal_report_threshold")
