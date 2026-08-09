"""Add event runtime, monitored sources, onboarding and report versions.

Revision ID: 20260809_0013
Revises: 20260808_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0013"
down_revision: str | None = "20260808_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_settings") as batch:
        batch.add_column(sa.Column("client_onboarding_json", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("report_config_json", sa.JSON(), nullable=False, server_default="{}"))
    op.execute("UPDATE tenant_settings SET client_onboarding_step = 'monitoring_started' WHERE client_onboarding_step = 'scope_selection'")
    op.execute("UPDATE tenant_settings SET client_onboarding_step = 'employees' WHERE client_onboarding_step = 'employees_review'")
    with op.batch_alter_table("telegram_connections") as batch:
        batch.add_column(sa.Column("runtime_status", sa.String(32), nullable=False, server_default="stopped"))
        batch.add_column(sa.Column("runtime_heartbeat_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("reconnect_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("updates_received", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("duplicate_events", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("catchup_events", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_telegram_connections_runtime_status", ["runtime_status"])
        batch.create_index("ix_telegram_connections_runtime_heartbeat_at", ["runtime_heartbeat_at"])
    with op.batch_alter_table("telegram_dialogs") as batch:
        batch.add_column(sa.Column("canonical_peer_id", sa.String(100), nullable=False, server_default=""))
        batch.create_index("ix_telegram_dialogs_canonical_peer_id", ["canonical_peer_id"])
    op.execute("UPDATE telegram_dialogs SET canonical_peer_id = CAST(telegram_dialog_id AS TEXT) WHERE canonical_peer_id = ''")
    with op.batch_alter_table("telegram_messages") as batch:
        batch.add_column(sa.Column("sender_role", sa.String(32), nullable=False, server_default="unknown"))
        batch.add_column(sa.Column("ingestion_source", sa.String(32), nullable=False, server_default="history"))
        batch.create_index("ix_telegram_messages_sender_role", ["sender_role"])
        batch.create_index("ix_telegram_messages_ingestion_source", ["ingestion_source"])
    with op.batch_alter_table("dialog_states") as batch:
        batch.add_column(sa.Column("last_report_version", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "monitored_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("telegram_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_peer_id", sa.String(100), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("added_via", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("telegram_link", sa.String(500)),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", "canonical_peer_id", name="uq_monitored_source_connection_peer"),
    )
    op.create_index("ix_monitored_sources_tenant_id", "monitored_sources", ["tenant_id"])
    op.create_index("ix_monitored_sources_connection_id", "monitored_sources", ["connection_id"])
    op.create_index("ix_monitored_sources_canonical_peer_id", "monitored_sources", ["canonical_peer_id"])
    op.create_index("ix_monitored_sources_source_type", "monitored_sources", ["source_type"])
    op.create_index("ix_monitored_sources_enabled", "monitored_sources", ["enabled"])

    op.create_table(
        "owner_client_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("platform_owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("raw_prompt_ciphertext", sa.LargeBinary()),
        sa.Column("draft_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("corrections_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("parser_provider", sa.String(64)),
        sa.Column("parser_model", sa.String(100)),
        sa.Column("confirmation_key", sa.String(100), nullable=False, unique=True),
        sa.Column("created_tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="SET NULL")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_owner_client_drafts_owner_id", "owner_client_drafts", ["owner_id"])
    op.create_index("ix_owner_client_drafts_status", "owner_client_drafts", ["status"])
    op.create_index("ix_owner_client_drafts_created_tenant_id", "owner_client_drafts", ["created_tenant_id"])


def downgrade() -> None:
    op.drop_table("owner_client_drafts")
    op.drop_table("monitored_sources")
    with op.batch_alter_table("dialog_states") as batch:
        batch.drop_column("last_report_version")
    with op.batch_alter_table("telegram_messages") as batch:
        batch.drop_index("ix_telegram_messages_ingestion_source")
        batch.drop_index("ix_telegram_messages_sender_role")
        batch.drop_column("ingestion_source")
        batch.drop_column("sender_role")
    with op.batch_alter_table("telegram_dialogs") as batch:
        batch.drop_index("ix_telegram_dialogs_canonical_peer_id")
        batch.drop_column("canonical_peer_id")
    with op.batch_alter_table("telegram_connections") as batch:
        batch.drop_index("ix_telegram_connections_runtime_heartbeat_at")
        batch.drop_index("ix_telegram_connections_runtime_status")
        for name in ("catchup_events", "duplicate_events", "updates_received", "reconnect_count", "runtime_heartbeat_at", "runtime_status"):
            batch.drop_column(name)
    with op.batch_alter_table("tenant_settings") as batch:
        batch.drop_column("report_config_json")
        batch.drop_column("client_onboarding_json")
