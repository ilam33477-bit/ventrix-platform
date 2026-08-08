"""Encrypted Telegram connections and resumable initial analysis.

Revision ID: 20260804_0004
Revises: 20260804_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0004"
down_revision: str | None = "20260804_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def common() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def indexes(table: str, names: list[str]) -> None:
    for name in names:
        op.create_index(f"ix_{table}_{name}", table, [name])


def upgrade() -> None:
    op.create_table(
        "telegram_connections",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_secret_id",
            sa.String(36),
            sa.ForeignKey("encrypted_secrets.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column(
            "pending_session_secret_id",
            sa.String(36),
            sa.ForeignKey("encrypted_secrets.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column(
            "phone_secret_id",
            sa.String(36),
            sa.ForeignKey("encrypted_secrets.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column(
            "phone_code_hash_secret_id",
            sa.String(36),
            sa.ForeignKey("encrypted_secrets.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("username", sa.String(64)),
        sa.Column("display_name", sa.String(200)),
        sa.Column("phone_masked", sa.String(32)),
        sa.Column("status", sa.String(32), server_default="disconnected", nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("consent_version", sa.String(32)),
        sa.Column("personal_dialogs_consent", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("history_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("selected_folder_id", sa.Integer()),
        sa.Column("selected_folder_title", sa.String(200)),
        sa.Column("progress_stage", sa.String(64), server_default="not_started", nullable=False),
        sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_json", sa.JSON(), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(100)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "history_days IN (3,7,14,30)", name="ck_telegram_connections_history_days"
        ),
        sa.CheckConstraint(
            "progress_percent BETWEEN 0 AND 100", name="ck_telegram_connections_progress"
        ),
    )
    indexes("telegram_connections", ["tenant_id", "telegram_user_id", "status", "deleted_at"])
    op.create_index(
        "ix_telegram_connections_tenant_active", "telegram_connections", ["tenant_id", "deleted_at"]
    )

    op.create_table(
        "telegram_folders",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_folder_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("chat_count", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint(
            "connection_id", "telegram_folder_id", name="uq_telegram_folder_remote"
        ),
    )
    indexes("telegram_folders", ["tenant_id", "connection_id"])

    op.create_table(
        "telegram_dialogs",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_dialog_id", sa.BigInteger(), nullable=False),
        sa.Column("folder_id", sa.Integer()),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("dialog_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("classification", sa.String(64), server_default="unknown", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("requires_user_confirmation", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("selected", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("excluded", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("participants_count", sa.Integer()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "connection_id", "telegram_dialog_id", name="uq_telegram_dialog_remote"
        ),
    )
    indexes(
        "telegram_dialogs",
        [
            "tenant_id",
            "connection_id",
            "folder_id",
            "dialog_type",
            "source",
            "classification",
            "selected",
            "excluded",
        ],
    )

    op.create_table(
        "initial_analysis_runs",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("stage", sa.String(64), server_default="queued", nullable=False),
        sa.Column("history_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_dialogs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_dialogs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_dialogs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("messages_loaded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("progress_chat_id", sa.BigInteger()),
        sa.Column("progress_message_id", sa.BigInteger()),
        sa.Column("stop_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(100)),
        sa.UniqueConstraint("connection_id", "generation", name="uq_initial_analysis_generation"),
        sa.CheckConstraint(
            "progress_percent BETWEEN 0 AND 100", name="ck_initial_analysis_progress"
        ),
    )
    indexes("initial_analysis_runs", ["tenant_id", "connection_id", "status"])

    op.create_table(
        "telegram_sync_cursors",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("initial_analysis_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dialog_id",
            sa.String(36),
            sa.ForeignKey("telegram_dialogs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("offset_message_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("fetched_messages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_batch_at", sa.DateTime(timezone=True)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(100)),
        sa.UniqueConstraint("run_id", "dialog_id", name="uq_sync_cursor_run_dialog"),
    )
    indexes(
        "telegram_sync_cursors",
        ["tenant_id", "run_id", "connection_id", "dialog_id", "status", "retry_after"],
    )

    op.create_table(
        "telegram_messages",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dialog_id",
            sa.String(36),
            sa.ForeignKey("telegram_dialogs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger()),
        sa.Column("sender_username", sa.String(64)),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("outgoing", sa.Boolean(), nullable=False),
        sa.Column("body_text", sa.Text()),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("dialog_id", "telegram_message_id", name="uq_telegram_message_remote"),
    )
    indexes(
        "telegram_messages",
        ["tenant_id", "connection_id", "dialog_id", "sender_id", "sent_at", "outgoing"],
    )

    op.create_table(
        "operational_problems",
        *common(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dialog_id",
            sa.String(36),
            sa.ForeignKey("telegram_dialogs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("telegram_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(200), unique=True, nullable=False),
        sa.Column("problem_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="open", nullable=False),
        sa.Column("priority", sa.String(16), server_default="medium", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    indexes(
        "operational_problems",
        [
            "tenant_id",
            "connection_id",
            "dialog_id",
            "source_message_id",
            "problem_type",
            "status",
            "priority",
            "occurred_at",
        ],
    )


def downgrade() -> None:
    for table in (
        "operational_problems",
        "telegram_messages",
        "telegram_sync_cursors",
        "initial_analysis_runs",
        "telegram_dialogs",
        "telegram_folders",
        "telegram_connections",
    ):
        op.drop_table(table)
