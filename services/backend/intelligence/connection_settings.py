from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import TelegramConnection, TenantSettings


@dataclass(frozen=True, slots=True)
class EffectiveConnectionSettings:
    response_sla_minutes: int
    signal_problem_threshold: int


async def effective_connection_settings(
    session: AsyncSession,
    *,
    tenant_id: str,
    connection_id: str | None,
    tenant_settings: TenantSettings | None = None,
) -> EffectiveConnectionSettings:
    """Resolve tenant defaults with optional per-session analysis overrides."""
    settings = tenant_settings or await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    )
    if settings is None:
        raise LookupError("tenant settings not found")
    connection = await session.get(TelegramConnection, connection_id) if connection_id else None
    if connection is not None and connection.tenant_id != tenant_id:
        connection = None
    return EffectiveConnectionSettings(
        response_sla_minutes=(
            connection.response_sla_minutes_override
            if connection and connection.response_sla_minutes_override is not None
            else settings.response_sla_minutes
        ),
        signal_problem_threshold=(
            connection.signal_problem_threshold_override
            if connection and connection.signal_problem_threshold_override is not None
            else settings.signal_problem_threshold
        ),
    )
