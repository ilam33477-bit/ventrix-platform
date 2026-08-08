"""Initial single-server SQLite foundation.

Revision ID: 20260804_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def id_column() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "platform_owner",
        id_column(),
        *timestamps(),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(64)),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_platform_owner_telegram_id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_index("ix_platform_owner_telegram_user_id", "platform_owner", ["telegram_user_id"])

    op.create_table(
        "tenants",
        id_column(),
        *timestamps(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner_name", sa.String(200), nullable=False),
        sa.Column("owner_telegram_username", sa.String(64)),
        sa.Column("owner_telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("niche", sa.String(200), nullable=False),
        sa.Column("business_description", sa.Text(), nullable=False),
        sa.Column("products_services", sa.Text(), nullable=False),
        sa.Column("target_audience", sa.Text(), nullable=False),
        sa.Column("plan", sa.String(64), server_default="trial", nullable=False),
        sa.Column("subscription_expires_at", sa.Date()),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("owner_telegram_user_id > 0", name="ck_tenants_owner_telegram_id"),
    )
    for name, columns in (
        ("ix_tenants_name", ["name"]),
        ("ix_tenants_owner_telegram_username", ["owner_telegram_username"]),
        ("ix_tenants_owner_telegram_user_id", ["owner_telegram_user_id"]),
        ("ix_tenants_deleted_at", ["deleted_at"]),
        ("ix_tenants_status_not_deleted", ["status", "deleted_at"]),
    ):
        op.create_index(name, "tenants", columns)

    op.create_table(
        "tenant_settings",
        id_column(),
        *timestamps(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("working_hours", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("response_sla_minutes", sa.Integer(), nullable=False),
        sa.Column("critical_problem_criteria", sa.Text(), nullable=False),
        sa.Column("daily_report_time", sa.Time(), nullable=False),
        sa.CheckConstraint("response_sla_minutes > 0", name="ck_tenant_settings_sla"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"])

    op.create_table(
        "tenant_ai_profiles",
        id_column(),
        *timestamps(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("niche", sa.String(200), nullable=False),
        sa.Column("business_description", sa.Text(), nullable=False),
        sa.Column("products", sa.JSON(), nullable=False),
        sa.Column("target_audience", sa.Text(), nullable=False),
        sa.Column("typical_processes", sa.JSON(), nullable=False),
        sa.Column("sales_stages", sa.JSON(), nullable=False),
        sa.Column("typical_promises", sa.JSON(), nullable=False),
        sa.Column("typical_objections", sa.JSON(), nullable=False),
        sa.Column("critical_events", sa.JSON(), nullable=False),
        sa.Column("significant_amounts", sa.JSON(), nullable=False),
        sa.Column("response_sla_minutes", sa.Integer(), nullable=False),
        sa.Column("prohibited_conclusions", sa.JSON(), nullable=False),
        sa.Column("additional_instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("response_sla_minutes > 0", name="ck_tenant_ai_profiles_sla"),
        sa.CheckConstraint("version > 0", name="ck_tenant_ai_profiles_version"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_ai_profiles_tenant_id", "tenant_ai_profiles", ["tenant_id"])

    op.create_table(
        "encrypted_secrets",
        id_column(),
        *timestamps(),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "kind", "fingerprint", name="uq_secret_scope_fingerprint"),
    )
    op.create_index("ix_encrypted_secrets_tenant_id", "encrypted_secrets", ["tenant_id"])
    op.create_index("ix_encrypted_secrets_kind", "encrypted_secrets", ["kind"])
    op.create_index("ix_encrypted_secrets_deleted_at", "encrypted_secrets", ["deleted_at"])

    op.create_table(
        "bot_instances",
        id_column(),
        *timestamps(),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "secret_id",
            sa.String(36),
            sa.ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("verification_status", sa.String(32), server_default="verified", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("telegram_bot_id > 0", name="ck_bot_instances_telegram_id"),
        sa.UniqueConstraint("secret_id"),
        sa.UniqueConstraint("telegram_bot_id"),
        sa.UniqueConstraint("username"),
    )
    for name, columns in (
        ("ix_bot_instances_tenant_id", ["tenant_id"]),
        ("ix_bot_instances_telegram_bot_id", ["telegram_bot_id"]),
        ("ix_bot_instances_username", ["username"]),
        ("ix_bot_instances_verification_status", ["verification_status"]),
        ("ix_bot_instances_deleted_at", ["deleted_at"]),
        ("ix_bot_instances_tenant_active", ["tenant_id", "is_active"]),
    ):
        op.create_index(name, "bot_instances", columns)

    op.create_table(
        "audit_logs",
        id_column(),
        *timestamps(),
        sa.Column(
            "actor_owner_id",
            sa.String(36),
            sa.ForeignKey("platform_owner.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(36)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
    )
    for name, columns in (
        ("ix_audit_logs_actor_owner_id", ["actor_owner_id"]),
        ("ix_audit_logs_tenant_id", ["tenant_id"]),
        ("ix_audit_logs_action", ["action"]),
        ("ix_audit_logs_entity_id", ["entity_id"]),
        ("ix_audit_logs_tenant_created", ["tenant_id", "created_at"]),
    ):
        op.create_index(name, "audit_logs", columns)

    op.create_table(
        "fsm_states",
        id_column(),
        *timestamps(),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("business_connection_id", sa.String(128), server_default="", nullable=False),
        sa.Column("destiny", sa.String(128), server_default="default", nullable=False),
        sa.Column("state", sa.String(255)),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "bot_id",
            "chat_id",
            "user_id",
            "thread_id",
            "business_connection_id",
            "destiny",
            name="uq_fsm_storage_key",
        ),
    )
    op.create_index("ix_fsm_states_expires_at", "fsm_states", ["expires_at"])
    op.create_index("ix_fsm_states_user_bot", "fsm_states", ["user_id", "bot_id"])

    op.create_table(
        "background_jobs",
        id_column(),
        *timestamps(),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("idempotency_key", sa.String(200), unique=True),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("locked_by", sa.String(100)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','retry_scheduled','cancelled')",
            name="ck_background_jobs_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0", name="ck_background_jobs_attempts"
        ),
    )
    for name, columns in (
        ("ix_background_jobs_tenant_id", ["tenant_id"]),
        ("ix_background_jobs_job_type", ["job_type"]),
        ("ix_background_jobs_status", ["status"]),
        ("ix_background_jobs_scheduled_at", ["scheduled_at"]),
        ("ix_background_jobs_locked_by", ["locked_by"]),
        ("ix_background_jobs_locked_at", ["locked_at"]),
        ("ix_background_jobs_available", ["status", "scheduled_at", "priority"]),
        ("ix_background_jobs_tenant_type", ["tenant_id", "job_type"]),
    ):
        op.create_index(name, "background_jobs", columns)


def downgrade() -> None:
    for table in (
        "background_jobs",
        "fsm_states",
        "audit_logs",
        "bot_instances",
        "encrypted_secrets",
        "tenant_ai_profiles",
        "tenant_settings",
        "tenants",
        "platform_owner",
    ):
        op.drop_table(table)
