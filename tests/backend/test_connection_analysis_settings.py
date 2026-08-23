from __future__ import annotations

import pytest
from sqlalchemy import select

from services.backend.intelligence.connection_settings import effective_connection_settings
from services.backend.models import TelegramConnection, TenantSettings


@pytest.mark.asyncio
async def test_connection_analysis_settings_override_tenant_defaults(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        connection = TelegramConnection(
            tenant_id=tenant.id,
            status="ready",
            history_days=14,
            response_sla_minutes_override=30,
            signal_problem_threshold_override=78,
        )
        session.add(connection)
        await session.commit()

        effective = await effective_connection_settings(
            session,
            tenant_id=tenant.id,
            connection_id=connection.id,
            tenant_settings=settings,
        )
        defaults = await effective_connection_settings(
            session,
            tenant_id=tenant.id,
            connection_id=None,
            tenant_settings=settings,
        )

    assert effective.response_sla_minutes == 30
    assert effective.signal_problem_threshold == 78
    assert defaults.response_sla_minutes == tenant_payload.response_sla_minutes
    assert defaults.signal_problem_threshold == settings.signal_problem_threshold
