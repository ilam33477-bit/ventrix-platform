from __future__ import annotations

import json
import re
import time as monotonic_time
from datetime import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.deepseek import DeepSeekProvider

from ..models import OwnerClientDraft, PlatformOwner
from ..schemas import TenantCreate
from ..timezones import normalize_timezone
from .encryption import EncryptionService


class ClientDraftData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=200)
    owner_name: str = Field(default="Владелец проекта", min_length=2, max_length=200)
    owner_telegram_user_id: int = Field(gt=0)
    owner_telegram_username: str | None = None
    niche: str = Field(min_length=2, max_length=200)
    business_description: str = Field(min_length=5, max_length=10_000)
    products_services: str = Field(min_length=2, max_length=10_000)
    target_audience: str = Field(min_length=2, max_length=10_000)
    working_hours: dict[str, str] = Field(
        default_factory=lambda: {"description": "Пн–Пт, 09:00–18:00"}
    )
    timezone: str = "Europe/Moscow"
    response_sla_minutes: int = Field(default=60, gt=0, le=43_200)
    critical_problem_criteria: str = Field(min_length=2, max_length=10_000)
    daily_report_time: str = "09:00"
    plan: str = "trial"
    additional_ai_instructions: str = ""

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return normalize_timezone(value)

    @field_validator("owner_telegram_username")
    @classmethod
    def username(cls, value: str | None) -> str | None:
        return value.strip().lstrip("@").lower() if value else None

    def tenant_payload(self) -> TenantCreate:
        hour, minute = (int(part) for part in self.daily_report_time.split(":", 1))
        return TenantCreate(
            **self.model_dump(exclude={"daily_report_time"}),
            daily_report_time=time(hour, minute),
        )


CLIENT_DRAFT_SYSTEM_PROMPT = """
Ты product operations assistant Ventrix. Преобразуй свободное описание нового клиента
в один JSON-объект строго по переданной schema. Не выдумывай Telegram user ID: если его
нет, верни 0. Формируй практичные defaults для SLA, рабочего времени, критериев критичных
проблем и AI-инструкций. timezone всегда IANA. Не включай токены ботов, пароли и секреты.
При correction сохрани подтверждённые поля и измени только то, что просит владелец.
""".strip()

SECRET_PATTERN = re.compile(
    r"(?:\b\d{7,12}:[A-Za-z0-9_-]{20,}\b|\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"(?:session|string_session|2fa|парол)[^\n]{0,24}[=:]\s*\S+)",
    re.IGNORECASE,
)


class OwnerClientDraftService:
    def __init__(
        self,
        session: AsyncSession,
        provider: DeepSeekProvider,
        encryption: EncryptionService,
        model: str,
    ) -> None:
        self.session = session
        self.provider = provider
        self.encryption = encryption
        self.model = model

    async def create(self, owner_telegram_id: int, prompt: str) -> OwnerClientDraft:
        self._reject_secrets(prompt)
        owner = await self._owner(owner_telegram_id)
        data, latency_ms = await self._generate(prompt=prompt)
        draft = OwnerClientDraft(
            owner_id=owner.id,
            raw_prompt_ciphertext=self.encryption.encrypt(prompt),
            draft_json=data.model_dump(mode="json"),
            parser_provider="deepseek",
            parser_model=self.model,
            schema_version=1,
            prompt_version="client-draft-v1",
            parse_latency_ms=latency_ms,
            confirmation_key=f"client-draft:{uuid4()}",
        )
        self.session.add(draft)
        await self.session.commit()
        await self.session.refresh(draft)
        return draft

    async def correct(
        self, owner_telegram_id: int, draft_id: str, correction: str
    ) -> OwnerClientDraft:
        self._reject_secrets(correction)
        draft = await self._draft(owner_telegram_id, draft_id)
        before = dict(draft.draft_json)
        data, latency_ms = await self._generate(correction=correction, current=before)
        draft.draft_json = data.model_dump(mode="json")
        changed = {
            key: {"from": before.get(key), "to": value}
            for key, value in draft.draft_json.items()
            if before.get(key) != value
        }
        draft.corrections_json = [
            *draft.corrections_json,
            {"version": draft.version + 1, "changed_fields": sorted(changed)},
        ]
        draft.manual_changes_json = [
            *draft.manual_changes_json,
            {"version": draft.version + 1, "changes": changed},
        ]
        draft.parse_latency_ms = latency_ms
        draft.version += 1
        await self.session.commit()
        await self.session.refresh(draft)
        return draft

    async def _generate(
        self,
        *,
        prompt: str | None = None,
        correction: str | None = None,
        current: dict[str, Any] | None = None,
    ) -> tuple[ClientDraftData, int]:
        started = monotonic_time.perf_counter()
        content, _usage = await self.provider.generate_json(
            model=self.model,
            system_prompt=CLIENT_DRAFT_SYSTEM_PROMPT,
            payload={
                "schema": ClientDraftData.model_json_schema(),
                "description": prompt,
                "current_draft": current,
                "correction": correction,
            },
            max_tokens=3000,
        )
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("AI returned invalid JSON") from exc
        return ClientDraftData.model_validate(raw), int(
            (monotonic_time.perf_counter() - started) * 1000
        )

    @staticmethod
    def _reject_secrets(value: str) -> None:
        if SECRET_PATTERN.search(value):
            raise ValueError("Описание содержит секрет; удалите token/session/password")

    async def _owner(self, owner_telegram_id: int) -> PlatformOwner:
        owner = await self.session.scalar(
            select(PlatformOwner).where(PlatformOwner.telegram_user_id == owner_telegram_id)
        )
        if owner is None:
            owner = PlatformOwner(telegram_user_id=owner_telegram_id, is_active=True)
            self.session.add(owner)
            await self.session.flush()
        return owner

    async def _draft(self, owner_telegram_id: int, draft_id: str) -> OwnerClientDraft:
        draft = await self.session.scalar(
            select(OwnerClientDraft)
            .join(PlatformOwner, PlatformOwner.id == OwnerClientDraft.owner_id)
            .where(
                OwnerClientDraft.id == draft_id,
                PlatformOwner.telegram_user_id == owner_telegram_id,
                OwnerClientDraft.status == "draft",
            )
        )
        if draft is None:
            raise LookupError("Client draft not found")
        return draft
