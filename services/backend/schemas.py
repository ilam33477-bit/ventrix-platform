from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, computed_field, field_validator

from .timezones import normalize_timezone


def _clean_list(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


class TenantCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    owner_name: str = Field(min_length=2, max_length=200)
    owner_telegram_username: str | None = Field(default=None, max_length=64)
    owner_telegram_user_id: int = Field(gt=0)
    niche: str = Field(min_length=2, max_length=200)
    business_description: str = Field(min_length=5, max_length=10_000)
    products_services: str = Field(min_length=2, max_length=10_000)
    target_audience: str = Field(min_length=2, max_length=10_000)
    working_hours: dict[str, str]
    timezone: str = Field(min_length=3, max_length=64)
    response_sla_minutes: int = Field(gt=0, le=43_200)
    critical_problem_criteria: str = Field(min_length=2, max_length=10_000)
    daily_report_time: time
    plan: str = Field(default="trial", min_length=2, max_length=64)
    subscription_expires_at: date | None = None
    additional_ai_instructions: str = Field(default="", max_length=20_000)

    @field_validator("owner_telegram_username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().lstrip("@").lower()
        if len(normalized) < 5:
            raise ValueError("Telegram username is too short")
        return normalized

    @field_validator("timezone")
    @classmethod
    def normalize_tenant_timezone(cls, value: str) -> str:
        return normalize_timezone(value)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    owner_name: str | None = Field(default=None, min_length=2, max_length=200)
    owner_telegram_username: str | None = Field(default=None, min_length=5, max_length=64)
    owner_telegram_user_id: int | None = Field(default=None, gt=0)
    niche: str | None = Field(default=None, min_length=2, max_length=200)
    business_description: str | None = Field(default=None, min_length=5, max_length=10_000)
    products_services: str | None = Field(default=None, min_length=2, max_length=10_000)
    target_audience: str | None = Field(default=None, min_length=2, max_length=10_000)
    plan: str | None = Field(default=None, min_length=2, max_length=64)
    subscription_expires_at: date | None = None
    working_hours: dict[str, str] | None = None
    timezone: str | None = Field(default=None, min_length=3, max_length=64)
    response_sla_minutes: int | None = Field(default=None, gt=0, le=43_200)
    critical_problem_criteria: str | None = Field(default=None, min_length=2, max_length=10_000)
    daily_report_time: time | None = None

    @field_validator("timezone")
    @classmethod
    def normalize_tenant_timezone(cls, value: str | None) -> str | None:
        return normalize_timezone(value) if value is not None else None


class AIProfileUpdate(BaseModel):
    niche: str | None = Field(default=None, min_length=2, max_length=200)
    business_description: str | None = Field(default=None, min_length=5, max_length=10_000)
    products: list[str] | None = None
    target_audience: str | None = Field(default=None, min_length=2, max_length=10_000)
    typical_processes: list[str] | None = None
    sales_stages: list[str] | None = None
    typical_promises: list[str] | None = None
    typical_objections: list[str] | None = None
    critical_events: list[str] | None = None
    significant_amounts: list[int] | None = None
    response_sla_minutes: int | None = Field(default=None, gt=0, le=43_200)
    prohibited_conclusions: list[str] | None = None
    additional_instructions: str | None = Field(default=None, max_length=20_000)

    @field_validator(
        "products",
        "typical_processes",
        "sales_stages",
        "typical_promises",
        "typical_objections",
        "critical_events",
        "prohibited_conclusions",
    )
    @classmethod
    def normalize_lists(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_list(value)


class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    working_hours: dict[str, str]
    timezone: str
    response_sla_minutes: int
    critical_problem_criteria: str
    daily_report_time: time


class AIProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    niche: str
    business_description: str
    products: list[str]
    target_audience: str
    typical_processes: list[str]
    sales_stages: list[str]
    typical_promises: list[str]
    typical_objections: list[str]
    critical_events: list[str]
    significant_amounts: list[int]
    response_sla_minutes: int
    prohibited_conclusions: list[str]
    additional_instructions: str
    version: int
    created_at: datetime
    updated_at: datetime


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    owner_name: str
    owner_telegram_username: str | None
    owner_telegram_user_id: int
    niche: str
    business_description: str
    products_services: str
    target_audience: str
    plan: str
    subscription_expires_at: date | None
    status: str
    settings: SettingsRead
    created_at: datetime
    updated_at: datetime


class BotCreate(BaseModel):
    token: SecretStr

    @field_validator("token")
    @classmethod
    def validate_token_length(cls, value: SecretStr) -> SecretStr:
        length = len(value.get_secret_value())
        if length < 20 or length > 256:
            raise ValueError("invalid BotFather token length")
        return value


class BotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    telegram_bot_id: int
    username: str
    display_name: str
    verification_status: str
    verified_at: datetime
    is_active: bool
    created_at: datetime

    @computed_field
    @property
    def telegram_url(self) -> str:
        return f"https://t.me/{self.username}"
