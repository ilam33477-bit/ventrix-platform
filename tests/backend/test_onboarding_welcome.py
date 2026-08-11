from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from services.backend.models import AIUsageCall, Tenant
from services.backend.services.onboarding_welcome import ensure_onboarding_welcome


class WelcomeProvider:
    calls = 0

    async def generate_json(self, **_kwargs):
        self.calls += 1
        return (
            json.dumps(
                {
                    "headline": "Рабочие диалоги — под контролем",
                    "message": "Ventrix поможет команде вовремя замечать обращения и обещания. Начнём с подключения рабочего Telegram.",
                    "benefits": [
                        "Не терять обращения",
                        "Помнить о сроках",
                        "Видеть важные ситуации",
                    ],
                },
                ensure_ascii=False,
            ),
            {"input_tokens": 100, "output_tokens": 30},
        )


@pytest.mark.asyncio
async def test_ai_welcome_is_generated_once_and_cached(
    session_factory, make_service, tenant_payload
) -> None:
    provider = WelcomeProvider()
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        first = await ensure_onboarding_welcome(
            session, tenant, provider=provider, model="deepseek-test"
        )
        second = await ensure_onboarding_welcome(
            session, tenant, provider=provider, model="deepseek-test"
        )
        usage = await session.scalar(
            select(func.count(AIUsageCall.id)).where(
                AIUsageCall.tenant_id == tenant.id,
                AIUsageCall.job_type == "onboarding.welcome",
            )
        )
        stored = await session.get(Tenant, tenant.id)

    assert first == second
    assert provider.calls == 1
    assert usage == 1
    assert stored.settings.client_onboarding_json["_welcome_copy"]["headline"] == first.headline
