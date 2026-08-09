from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    platform_owner_telegram_id: int
    platform_owner_telegram_username: str | None = None
    telegram_owner_bot_token: SecretStr
    owner_api_token: SecretStr
    app_encryption_key: SecretStr
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_api_id: int | None = Field(default=None, gt=0)
    telegram_api_hash: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_fast_model: str = "deepseek-v4-flash"
    deepseek_deep_model: str = "deepseek-v4-pro"
    tenant_report_token_budget: int = Field(default=50_000, ge=1_000, le=2_000_000)
    deepseek_context_window_tokens: int = Field(default=64_000, ge=8_000, le=2_000_000)
    deepseek_max_output_tokens: int = Field(default=4_000, ge=500, le=64_000)
    deepseek_safety_margin_tokens: int = Field(default=8_000, ge=1_000, le=128_000)
    deepseek_max_dialogs_per_request: int = Field(default=12, ge=1, le=100)
    deepseek_dialog_overlap_tokens: int = Field(default=800, ge=0, le=8_000)
    telegram_sync_batch_size: int = Field(default=100, ge=10, le=500)
    telegram_sync_batch_pause_seconds: float = Field(default=1.0, ge=0.1, le=30)
    telegram_sync_max_messages_per_chat: int = Field(default=2000, ge=100, le=50_000)
    max_active_tenant_jobs: int = Field(default=2, ge=1, le=20)
    max_active_ai_requests: int = Field(default=2, ge=1, le=20)
    max_active_ai_fast_requests: int = Field(default=2, ge=1, le=20)
    max_active_ai_heavy_requests: int = Field(default=1, ge=1, le=10)
    max_active_telegram_requests: int = Field(default=1, ge=1, le=20)
    max_active_sync_jobs: int = Field(default=1, ge=1, le=20)
    max_active_report_jobs: int = Field(default=1, ge=1, le=20)
    max_active_notification_jobs: int = Field(default=1, ge=1, le=10)
    worker_general_concurrency: int = Field(default=1, ge=1, le=4)
    worker_realtime_concurrency: int = Field(default=2, ge=1, le=4)
    worker_telegram_concurrency: int = Field(default=1, ge=1, le=4)
    worker_ai_concurrency: int = Field(default=2, ge=1, le=4)
    worker_heavy_concurrency: int = Field(default=1, ge=1, le=2)
    worker_notification_concurrency: int = Field(default=1, ge=1, le=2)
    tenant_max_active_heavy_jobs: int = Field(default=1, ge=1, le=5)
    ai_request_timeout_seconds: int = Field(default=90, ge=10, le=600)
    telegram_request_timeout_seconds: int = Field(default=60, ge=10, le=600)
    scheduler_poll_interval_seconds: float = Field(default=30, ge=1, le=300)
    scheduler_heartbeat_timeout_seconds: int = Field(default=120, ge=10, le=900)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    fsm_ttl_hours: int = Field(default=72, ge=1, le=2160)
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    worker_lock_timeout_seconds: int = Field(default=300, ge=10, le=86_400)
    worker_id: str = Field(default="mvp-worker-1", min_length=3, max_length=100)
    worker_heartbeat_seconds: float = Field(default=10.0, ge=1, le=300)
    client_bot_sync_interval_seconds: float = Field(default=2.0, ge=0.2, le=60)
    client_bot_heartbeat_seconds: float = Field(default=15.0, ge=1, le=300)
    client_bot_restart_backoff_seconds: float = Field(default=2.0, ge=0.2, le=300)
    incremental_sync_interval_seconds: float = Field(default=900, ge=30, le=3600)
    hourly_reconciliation_interval_seconds: int = Field(default=3600, ge=300, le=86_400)
    signal_report_threshold: int = Field(default=40, ge=0, le=100)
    signal_problem_threshold: int = Field(default=65, ge=0, le=100)
    signal_immediate_threshold: int = Field(default=85, ge=0, le=100)
    notification_cooldown_minutes: int = Field(default=120, ge=1, le=10_080)
    signal_context_message_limit: int = Field(default=8, ge=3, le=20)
    client_mini_app_url: str | None = None
    platform_support_contact: str | None = None
    log_level: str = "INFO"

    @field_validator("signal_immediate_threshold")
    @classmethod
    def valid_signal_thresholds(cls, value: int, info: object) -> int:
        data = getattr(info, "data", {})
        report = int(data.get("signal_report_threshold", 40))
        problem = int(data.get("signal_problem_threshold", 65))
        if not report <= problem <= value:
            raise ValueError("signal thresholds must be ordered")
        return value

    @field_validator("platform_owner_telegram_id")
    @classmethod
    def positive_telegram_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PLATFORM_OWNER_TELEGRAM_ID must be positive")
        return value

    @field_validator("platform_owner_telegram_username")
    @classmethod
    def normalize_owner_username(cls, value: str | None) -> str | None:
        return value.strip().lstrip("@").lower() if value else None

    @field_validator("owner_api_token")
    @classmethod
    def strong_owner_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 24:
            raise ValueError("OWNER_API_TOKEN must contain at least 24 characters")
        return value

    @field_validator("app_encryption_key")
    @classmethod
    def valid_encryption_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("APP_ENCRYPTION_KEY is not a valid Fernet-sized key")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
