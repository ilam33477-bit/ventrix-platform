from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.deepseek import DeepSeekProvider

from ..models import AIUsageCall, Tenant

logger = logging.getLogger(__name__)

WELCOME_CACHE_KEY = "_welcome_copy"
WELCOME_SYSTEM_PROMPT = """
Ты продуктовый редактор Ventrix. Напиши тёплое, уверенное приветствие владельцу компании
перед первым подключением Telegram. Объясни пользу простыми словами: Ventrix помогает
не терять обращения, обещания и важные рабочие ситуации. Не показывай настройки,
внутренние AI-инструкции, thresholds, SLA, технические параметры или длинное описание
бизнеса. Не копируй входные данные дословно. Верни только JSON по schema. Русский язык.
headline — короткий заголовок. message — 2–3 коротких предложения. benefits — ровно
три коротких результата для бизнеса, без обещаний абсолютной точности.
""".strip()


class OnboardingWelcomeCopy(BaseModel):
    headline: str = Field(min_length=4, max_length=90)
    message: str = Field(min_length=30, max_length=500)
    benefits: list[str] = Field(min_length=3, max_length=3)


def fallback_welcome(tenant: Tenant) -> OnboardingWelcomeCopy:
    return OnboardingWelcomeCopy(
        headline=f"Добро пожаловать в Ventrix, {tenant.owner_name.split()[0]}",
        message=(
            f"Ventrix поможет команде {tenant.name} вовремя замечать важные ситуации "
            "в рабочих диалогах и возвращать их под контроль. Начнём с безопасного "
            "подключения рабочего Telegram."
        ),
        benefits=[
            "Не терять обращения без ответа",
            "Контролировать обещания и сроки",
            "Получать понятную картину по работе команды",
        ],
    )


async def ensure_onboarding_welcome(
    session: AsyncSession,
    tenant: Tenant,
    *,
    provider: DeepSeekProvider | None,
    model: str,
) -> OnboardingWelcomeCopy:
    onboarding = dict(tenant.settings.client_onboarding_json or {})
    cached = onboarding.get(WELCOME_CACHE_KEY)
    if isinstance(cached, dict):
        try:
            return OnboardingWelcomeCopy.model_validate(cached)
        except ValueError:
            pass
    if provider is None:
        return fallback_welcome(tenant)

    started = time.perf_counter()
    try:
        content, usage = await provider.generate_json(
            model=model,
            system_prompt=WELCOME_SYSTEM_PROMPT,
            payload={
                "schema": OnboardingWelcomeCopy.model_json_schema(),
                "company": tenant.name,
                "owner_first_name": tenant.owner_name.split()[0],
                "business_context": tenant.business_description,
                "audience": tenant.target_audience,
                "desired_outcomes": list(tenant.ai_profile.critical_events or [])[:5],
            },
            max_tokens=700,
        )
        copy = OnboardingWelcomeCopy.model_validate(json.loads(content))
    except Exception as exc:  # noqa: BLE001 - onboarding has a safe product fallback
        logger.warning(
            "AI onboarding welcome failed tenant_id=%s error=%s",
            tenant.id,
            type(exc).__name__,
        )
        return fallback_welcome(tenant)

    onboarding[WELCOME_CACHE_KEY] = copy.model_dump(mode="json")
    tenant.settings.client_onboarding_json = onboarding
    session.add(
        AIUsageCall(
            tenant_id=tenant.id,
            model=model,
            job_type="onboarding.welcome",
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="completed",
        )
    )
    await session.commit()
    return copy
