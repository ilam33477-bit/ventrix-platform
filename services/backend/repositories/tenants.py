from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import BotInstance, Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[Tenant]:
        result = await self.session.scalars(
            select(Tenant)
            .where(Tenant.deleted_at.is_(None))
            .options(selectinload(Tenant.settings), selectinload(Tenant.ai_profile))
            .order_by(Tenant.created_at.desc())
        )
        return list(result.unique())

    async def get(self, tenant_id: UUID | str) -> Tenant | None:
        return await self.session.scalar(
            select(Tenant)
            .where(Tenant.id == str(tenant_id), Tenant.deleted_at.is_(None))
            .options(
                selectinload(Tenant.settings),
                selectinload(Tenant.ai_profile),
                selectinload(Tenant.bots),
            )
        )

    async def list_bots(self, tenant_id: UUID | str) -> list[BotInstance]:
        result = await self.session.scalars(
            select(BotInstance)
            .where(BotInstance.tenant_id == str(tenant_id), BotInstance.deleted_at.is_(None))
            .order_by(BotInstance.created_at.desc())
        )
        return list(result)
