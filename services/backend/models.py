from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .database import Base
from .timezones import normalize_timezone


def new_id() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StringPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class PlatformOwner(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "platform_owner"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )

    __table_args__ = (
        CheckConstraint("telegram_user_id > 0", name="ck_platform_owner_telegram_id"),
    )


class Tenant(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    owner_name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_telegram_username: Mapped[str | None] = mapped_column(String(64), index=True)
    owner_telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    niche: Mapped[str] = mapped_column(String(200), nullable=False)
    business_description: Mapped[str] = mapped_column(Text, nullable=False)
    products_services: Mapped[str] = mapped_column(Text, nullable=False)
    target_audience: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(
        String(64), default="trial", server_default="trial", nullable=False
    )
    subscription_expires_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    settings: Mapped[TenantSettings] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    ai_profile: Mapped[TenantAIProfile] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    bots: Mapped[list[BotInstance]] = relationship(back_populates="tenant", lazy="selectin")

    __table_args__ = (
        CheckConstraint("owner_telegram_user_id > 0", name="ck_tenants_owner_telegram_id"),
        Index("ix_tenants_status_not_deleted", "status", "deleted_at"),
    )


class TenantSettings(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    working_hours: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    critical_problem_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    daily_report_time: Mapped[time] = mapped_column(Time, nullable=False)
    analysis_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    analysis_advance_minutes: Mapped[int] = mapped_column(
        Integer, default=10, server_default="10", nullable=False
    )
    enabled_days: Mapped[list[int]] = mapped_column(
        JSON, default=lambda: [0, 1, 2, 3, 4], server_default="[0,1,2,3,4]", nullable=False
    )
    history_window_days: Mapped[int] = mapped_column(
        Integer, default=7, server_default="7", nullable=False
    )
    signal_report_threshold: Mapped[int] = mapped_column(
        Integer, default=40, server_default="40", nullable=False
    )
    signal_problem_threshold: Mapped[int] = mapped_column(
        Integer, default=65, server_default="65", nullable=False
    )
    signal_immediate_threshold: Mapped[int] = mapped_column(
        Integer, default=85, server_default="85", nullable=False
    )
    manager_notification_threshold: Mapped[int] = mapped_column(
        Integer, default=65, server_default="65", nullable=False
    )
    employee_notification_threshold: Mapped[int] = mapped_column(
        Integer, default=70, server_default="70", nullable=False
    )
    group_notification_threshold: Mapped[int] = mapped_column(
        Integer, default=85, server_default="85", nullable=False
    )
    notification_immediate_threshold: Mapped[int] = mapped_column(
        Integer, default=90, server_default="90", nullable=False
    )
    critical_fast_lane_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]", nullable=False
    )
    ai_daily_soft_limit: Mapped[int | None] = mapped_column(Integer)
    ai_daily_hard_limit: Mapped[int | None] = mapped_column(Integer)
    employee_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    group_reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    client_onboarding_step: Mapped[str] = mapped_column(
        String(32), default="welcome", server_default="welcome", nullable=False
    )
    client_onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_onboarding_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    report_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )

    @validates("timezone")
    def validate_timezone(self, _key: str, value: str) -> str:
        return normalize_timezone(value)

    tenant: Mapped[Tenant] = relationship(back_populates="settings")
    __table_args__ = (
        CheckConstraint("response_sla_minutes > 0", name="ck_tenant_settings_sla"),
        CheckConstraint(
            "analysis_advance_minutes IN (5,10,15,30)",
            name="ck_tenant_settings_analysis_advance",
        ),
        CheckConstraint(
            "history_window_days IN (3,7,14,30)",
            name="ck_tenant_settings_history_window",
        ),
        CheckConstraint(
            "signal_report_threshold BETWEEN 0 AND 100 AND "
            "signal_problem_threshold BETWEEN signal_report_threshold AND 100 AND "
            "signal_immediate_threshold BETWEEN signal_problem_threshold AND 100",
            name="ck_tenant_settings_signal_thresholds",
        ),
        CheckConstraint(
            "manager_notification_threshold BETWEEN 0 AND 100 AND "
            "employee_notification_threshold BETWEEN 0 AND 100 AND "
            "group_notification_threshold BETWEEN 0 AND 100 AND "
            "notification_immediate_threshold BETWEEN 0 AND 100",
            name="ck_tenant_settings_notification_thresholds",
        ),
        CheckConstraint(
            "ai_daily_soft_limit IS NULL OR ai_daily_soft_limit > 0",
            name="ck_tenant_settings_ai_soft_limit",
        ),
        CheckConstraint(
            "ai_daily_hard_limit IS NULL OR ai_daily_hard_limit > 0",
            name="ck_tenant_settings_ai_hard_limit",
        ),
    )


class TenantAIProfile(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_ai_profiles"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    niche: Mapped[str] = mapped_column(String(200), nullable=False)
    business_description: Mapped[str] = mapped_column(Text, nullable=False)
    products: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    target_audience: Mapped[str] = mapped_column(Text, nullable=False)
    typical_processes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    sales_stages: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    typical_promises: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    typical_objections: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    critical_events: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    significant_amounts: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    response_sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    prohibited_conclusions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    additional_instructions: Mapped[str] = mapped_column(Text, default="", server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="ai_profile")
    __table_args__ = (
        CheckConstraint("response_sla_minutes > 0", name="ck_tenant_ai_profiles_sla"),
        CheckConstraint("version > 0", name="ck_tenant_ai_profiles_version"),
    )


class EncryptedSecret(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "encrypted_secrets"

    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "fingerprint", name="uq_secret_scope_fingerprint"),
    )


class BotInstance(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_instances"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    secret_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"), unique=True
    )
    telegram_bot_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), default="verified", server_default="verified", nullable=False, index=True
    )
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    runtime_status: Mapped[str] = mapped_column(
        String(32), default="stopped", server_default="stopped", nullable=False, index=True
    )
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    processed_updates: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    button_clicks: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    runtime_restart_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    runtime_generation: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    tenant: Mapped[Tenant] = relationship(back_populates="bots")
    secret: Mapped[EncryptedSecret] = relationship(lazy="joined")
    __table_args__ = (
        CheckConstraint("telegram_bot_id > 0", name="ck_bot_instances_telegram_id"),
        CheckConstraint(
            "runtime_status IN ('stopped','starting','running','failed','stopping')",
            name="ck_bot_instances_runtime_status",
        ),
        CheckConstraint(
            "processed_updates >= 0 AND button_clicks >= 0 AND runtime_restart_count >= 0 "
            "AND runtime_generation > 0",
            name="ck_bot_instances_runtime_counters",
        ),
        Index("ix_bot_instances_tenant_active", "tenant_id", "is_active"),
    )


class AuditLog(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    actor_owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_owner.id", ondelete="RESTRICT"), index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="system", nullable=False)

    __table_args__ = (Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),)


class OwnerClientDraft(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "owner_client_drafts"

    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("platform_owner.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="draft", server_default="draft", nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    prompt_version: Mapped[str] = mapped_column(
        String(32), default="client-draft-v1", server_default="client-draft-v1", nullable=False
    )
    parse_latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_prompt_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    corrections_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    manual_changes_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    parser_provider: Mapped[str | None] = mapped_column(String(64))
    parser_model: Mapped[str | None] = mapped_column(String(100))
    confirmation_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_actor_id: Mapped[int | None] = mapped_column(BigInteger)


class ProductEvent(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_events"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bot_instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("bot_instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    event_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_product_events_bot_occurred", "bot_instance_id", "occurred_at"),
        Index("ix_product_events_tenant_event", "tenant_id", "event_name"),
    )


class TelegramConnection(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_connections"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    session_secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("encrypted_secrets.id", ondelete="SET NULL"), unique=True
    )
    pending_session_secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("encrypted_secrets.id", ondelete="SET NULL"), unique=True
    )
    phone_secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("encrypted_secrets.id", ondelete="SET NULL"), unique=True
    )
    phone_code_hash_secret_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("encrypted_secrets.id", ondelete="SET NULL"), unique=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(200))
    phone_masked: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32), default="disconnected", server_default="disconnected", index=True
    )
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str | None] = mapped_column(String(32))
    personal_dialogs_consent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    history_days: Mapped[int] = mapped_column(
        Integer, default=7, server_default="7", nullable=False
    )
    selected_folder_id: Mapped[int | None] = mapped_column(Integer)
    selected_folder_ids: Mapped[list[int]] = mapped_column(
        JSON, default=list, server_default="[]", nullable=False
    )
    selected_folder_title: Mapped[str | None] = mapped_column(String(200))
    progress_stage: Mapped[str] = mapped_column(
        String(64), default="not_started", server_default="not_started"
    )
    progress_percent: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    progress_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    stop_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_incremental_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incremental_state_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    error_state: Mapped[str | None] = mapped_column(String(200))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(
        String(32), default="unknown", server_default="unknown", nullable=False, index=True
    )
    runtime_status: Mapped[str] = mapped_column(
        String(32), default="stopped", server_default="stopped", nullable=False, index=True
    )
    runtime_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updates_received: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duplicate_events: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    catchup_events: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    edited_updates_received: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_reconnect_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_catchup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        CheckConstraint("history_days IN (3,7,14,30)", name="ck_telegram_connections_history_days"),
        CheckConstraint(
            "progress_percent BETWEEN 0 AND 100", name="ck_telegram_connections_progress"
        ),
        Index("ix_telegram_connections_tenant_active", "tenant_id", "deleted_at"),
    )


class TelegramRuntimeLease(TimestampMixin, Base):
    __tablename__ = "telegram_runtime_leases"

    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), primary_key=True
    )
    owner_instance_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelegramFolder(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_folders"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    telegram_folder_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    chat_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    __table_args__ = (
        UniqueConstraint("connection_id", "telegram_folder_id", name="uq_telegram_folder_remote"),
    )


class TelegramDialog(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_dialogs"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    telegram_dialog_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_peer_id: Mapped[str] = mapped_column(
        String(100), default="", server_default="", nullable=False, index=True
    )
    folder_id: Mapped[int | None] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    dialog_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    classification: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown", index=True
    )
    confidence: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0", nullable=False
    )
    requires_user_confirmation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    selected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    excluded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    participants_count: Mapped[int | None] = mapped_column(Integer)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        UniqueConstraint("connection_id", "telegram_dialog_id", name="uq_telegram_dialog_remote"),
    )

    @validates("telegram_dialog_id")
    def derive_canonical_peer_id(self, _key: str, value: int) -> int:
        if not self.canonical_peer_id:
            self.canonical_peer_id = str(value)
        return value


class InitialAnalysisRun(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "initial_analysis_runs"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued", server_default="queued")
    history_days: Mapped[int] = mapped_column(
        Integer, default=7, server_default="7", nullable=False
    )
    progress_percent: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_dialogs: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    completed_dialogs: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    failed_dialogs: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    messages_loaded: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    progress_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger)
    stop_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("connection_id", "generation", name="uq_initial_analysis_generation"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="ck_initial_analysis_progress"),
    )


class TelegramSyncCursor(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_sync_cursors"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("initial_analysis_runs.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    offset_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    fetched_messages: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_batch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("run_id", "dialog_id", name="uq_sync_cursor_run_dialog"),)


class TelegramMessage(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_messages"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    sender_username: Mapped[str | None] = mapped_column(String(64))
    sender_role: Mapped[str] = mapped_column(
        String(32), default="unknown", server_default="unknown", nullable=False, index=True
    )
    ingestion_source: Mapped[str] = mapped_column(
        String(32), default="history", server_default="history", nullable=False, index=True
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outgoing: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    body_text: Mapped[str | None] = mapped_column(Text)
    attachments_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("dialog_id", "telegram_message_id", name="uq_telegram_message_remote"),
    )


class TelegramIncrementalCursor(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_incremental_cursors"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), default="idle", server_default="idle", nullable=False, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("connection_id", "dialog_id", name="uq_incremental_cursor_dialog"),
    )


class Signal(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signals"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    telegram_connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_messages.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    local_score: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ai_score: Mapped[int | None] = mapped_column(Integer, index=True)
    criticality: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="candidate", server_default="candidate", nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint("local_score BETWEEN 0 AND 100", name="ck_signals_local_score"),
        CheckConstraint(
            "ai_score IS NULL OR ai_score BETWEEN 0 AND 100", name="ck_signals_ai_score"
        ),
        CheckConstraint("criticality BETWEEN 0 AND 100", name="ck_signals_criticality"),
        Index("ix_signals_tenant_status_detected", "tenant_id", "status", "detected_at"),
    )


class DialogState(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dialog_states"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), unique=True, index=True
    )
    relationship_type: Mapped[str] = mapped_column(
        String(64), default="unknown", server_default="unknown", nullable=False
    )
    compact_summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    open_commitments_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    unresolved_questions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    last_customer_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_employee_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_ai_processed_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    meaningful_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_report_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    awaiting_employee_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    awaiting_customer_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_expected_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("telegram_messages.id", ondelete="SET NULL")
    )
    next_sla_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_employee_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_customer_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonitoredSource(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "monitored_sources"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    canonical_peer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    added_via: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    telegram_link: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "canonical_peer_id", name="uq_monitored_source_connection_peer"
        ),
    )


class Commitment(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "commitments"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_messages.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    responsible_employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    commitment_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expected_action: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="open", server_default="open", nullable=False, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_commitments_confidence"),
        Index("ix_commitments_tenant_status_deadline", "tenant_id", "status", "deadline_at"),
    )


class OperationalProblem(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_problems"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_messages.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    commitment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("commitments.id", ondelete="SET NULL"), index=True
    )
    responsible_employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(200), unique=True)
    problem_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="new", server_default="new", nullable=False, index=True
    )
    priority: Mapped[str] = mapped_column(
        String(16), default="medium", server_default="medium", index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(Text)
    resolution_evidence: Mapped[str | None] = mapped_column(Text)


class ProblemTransition(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "problem_transitions"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operational_problems.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class ProblemVerification(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "problem_verifications"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operational_problems.id", ondelete="CASCADE"), index=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_message_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_problem_verification_confidence"),
    )


class FSMState(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "fsm_states"

    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    business_connection_id: Mapped[str] = mapped_column(
        String(128), default="", server_default="", nullable=False
    )
    destiny: Mapped[str] = mapped_column(
        String(128), default="default", server_default="default", nullable=False
    )
    state: Mapped[str | None] = mapped_column(String(255))
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint(
            "bot_id",
            "chat_id",
            "user_id",
            "thread_id",
            "business_connection_id",
            "destiny",
            name="uq_fsm_storage_key",
        ),
        Index("ix_fsm_states_user_bot", "user_id", "bot_id"),
    )


class BackgroundJob(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "background_jobs"

    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    telegram_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="SET NULL"), index=True
    )
    dialog_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="SET NULL"), index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    progress_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    is_heavy: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(
        String(32), default="general", server_default="general", nullable=False, index=True
    )
    cost_class: Mapped[str] = mapped_column(
        String(16), default="light", server_default="light", nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    priority: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3", nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[str | None] = mapped_column(String(100), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delay_reason: Mapped[str | None] = mapped_column(String(200))
    partition_key: Mapped[str | None] = mapped_column(String(200), index=True)
    partition_sequence: Mapped[int | None] = mapped_column(BigInteger, index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','scheduled','running','waiting','retry','retry_scheduled',"
            "'completed','failed','cancelled')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="ck_background_jobs_attempts"),
        Index("ix_background_jobs_available", "status", "scheduled_at", "priority"),
        Index("ix_background_jobs_tenant_type", "tenant_id", "job_type"),
        Index("ix_background_jobs_fair_claim", "status", "priority", "scheduled_at", "tenant_id"),
        Index(
            "ix_background_jobs_partition_order",
            "partition_key",
            "partition_sequence",
            "status",
        ),
    )


class TenantAnalysisSchedule(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_analysis_schedules"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    report_time: Mapped[time] = mapped_column(Time, nullable=False)
    enabled_days: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    history_window_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    advance_minutes: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    analysis_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False, index=True
    )
    next_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    access_status: Mapped[str] = mapped_column(
        String(32), default="active", server_default="active", nullable=False, index=True
    )
    grace_period_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("timezone")
    def validate_timezone(self, _key: str, value: str) -> str:
        return normalize_timezone(value)

    __table_args__ = (
        CheckConstraint("history_window_days IN (3,7,14,30)", name="ck_schedule_history"),
        CheckConstraint("advance_minutes IN (5,10,15,30)", name="ck_schedule_advance"),
    )


class TenantQueueState(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_queue_states"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    active_heavy_jobs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AnalysisRun(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_runs"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    telegram_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="SET NULL"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued", server_default="queued")
    report_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delayed_reason: Mapped[str | None] = mapped_column(String(200))
    required_batches: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_batches: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_batches: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    token_budget: Mapped[int] = mapped_column(Integer, default=50_000, server_default="50000")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class AnalysisBatch(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_batches"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0", server_default="1.0")
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    route_name: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    local_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    raw_response_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    repair_attempted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("run_id", "dialog_id", name="uq_analysis_batch_run_dialog"),)


class Report(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reports"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    analysis_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_runs.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="forming", server_default="forming", index=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")


class ReportSection(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_sections"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("report_id", "section_key", name="uq_report_section"),)


class ReportMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_metrics"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    numeric_value: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("report_id", "metric_key", name="uq_report_metric"),)


class ReportProblem(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_problems"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operational_problems.id", ondelete="CASCADE"), index=True
    )
    __table_args__ = (UniqueConstraint("report_id", "problem_id", name="uq_report_problem"),)


class ReportGenerationRun(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_generation_runs"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delayed_reason: Mapped[str | None] = mapped_column(String(200))


class Department(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "departments"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_department_name"),)


class Employee(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employees"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    department_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(
        String(64), default="employee", server_default="employee", nullable=False, index=True
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    criticality_threshold: Mapped[int] = mapped_column(
        Integer, default=85, server_default="85", nullable=False
    )
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    __table_args__ = (
        UniqueConstraint("tenant_id", "telegram_user_id", name="uq_employee_direct_telegram"),
        CheckConstraint("criticality_threshold BETWEEN 0 AND 100", name="ck_employee_criticality"),
    )


class GroupIntegration(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "group_integrations"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    bot_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("bot_instances.id", ondelete="SET NULL"), index=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False, index=True
    )
    participants_count: Mapped[int | None] = mapped_column(Integer)
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    minimum_criticality: Mapped[int] = mapped_column(
        Integer, default=85, server_default="85", nullable=False
    )
    reminder_cooldown_minutes: Mapped[int] = mapped_column(
        Integer, default=120, server_default="120", nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "telegram_chat_id", name="uq_group_integration_chat"),
        CheckConstraint(
            "minimum_criticality BETWEEN 0 AND 100", name="ck_group_minimum_criticality"
        ),
        CheckConstraint("reminder_cooldown_minutes > 0", name="ck_group_reminder_cooldown"),
    )


class NotificationLog(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_logs"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    problem_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("operational_problems.id", ondelete="SET NULL"), index=True
    )
    commitment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("commitments.id", ondelete="SET NULL"), index=True
    )
    employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    group_integration_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_integrations.id", ondelete="SET NULL"), index=True
    )
    destination_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    destination_id: Mapped[str] = mapped_column(String(100), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False, index=True
    )
    criticality: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("criticality BETWEEN 0 AND 100", name="ck_notification_criticality"),
        Index("ix_notification_destination_sent", "tenant_id", "destination_type", "sent_at"),
    )


class EmployeeTelegramAccount(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employee_telegram_accounts"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("tenant_id", "telegram_user_id", name="uq_employee_telegram"),
    )


class EmployeeRole(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employee_roles"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("employee_id", "role", name="uq_employee_role"),)


class TenantMembership(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_memberships"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    employee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="active", server_default="active", nullable=False, index=True
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "telegram_user_id", name="uq_tenant_membership"),
    )


class Permission(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "permissions"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    membership_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenant_memberships.id", ondelete="CASCADE"), index=True
    )
    permission: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (
        UniqueConstraint("membership_id", "permission", name="uq_membership_permission"),
    )


class AIUsageMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_usage_metrics"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    analysis_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("analysis_runs.id", ondelete="SET NULL"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    recheck_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    discarded_candidates: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("tenant_id", "metric_date", "model", name="uq_ai_usage_daily_model"),
    )


class AIUsageCall(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_usage_calls"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("background_jobs.id", ondelete="SET NULL"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class TenantDailyMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_daily_metrics"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "metric_date", name="uq_tenant_daily"),)


class EmployeeDailyMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employee_daily_metrics"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("employee_id", "metric_date", name="uq_employee_daily"),)


class ChatDailyMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_daily_metrics"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    dialog_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_dialogs.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("dialog_id", "metric_date", name="uq_chat_daily"),)


class SyncMetric(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sync_metrics"
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("telegram_connections.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("connection_id", "metric_date", name="uq_sync_daily"),)


class RuntimeHealth(StringPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runtime_health"

    component: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
