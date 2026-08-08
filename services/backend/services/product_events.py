from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..models import BotInstance, ProductEvent

BUTTON_EVENTS = {
    "summary_opened",
    "problems_opened",
    "reports_opened",
    "settings_opened",
    "miniapp_button_clicked",
    "telegram_connection_started",
    "onboarding_started",
    "button_clicked",
}


async def add_system_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_name: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Append a tenant event when its active client bot exists."""
    bot_id = await session.scalar(
        select(BotInstance.id)
        .where(
            BotInstance.tenant_id == tenant_id,
            BotInstance.enabled.is_(True),
            BotInstance.deleted_at.is_(None),
        )
        .order_by(BotInstance.created_at.desc())
        .limit(1)
    )
    if bot_id is None:
        return False
    safe_metadata = {
        key: value
        for key, value in (metadata or {}).items()
        if not any(word in key.lower() for word in ("token", "secret", "password", "code"))
    }
    session.add(
        ProductEvent(
            tenant_id=tenant_id,
            bot_instance_id=bot_id,
            event_name=event_name,
            occurred_at=datetime.now(UTC),
            metadata_json=safe_metadata,
        )
    )
    return True


@dataclass(frozen=True, slots=True)
class BotEventStats:
    unique_users: int
    total_events: int
    events_last_24h: int
    last_event_name: str | None
    last_event_at: datetime | None
    popular_buttons: list[tuple[str, int]]


class ProductEventService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)

    async def touch_update(self, *, tenant_id: str, bot_instance_id: str) -> None:
        async def write(session: AsyncSession) -> None:
            changed = await session.execute(
                update(BotInstance)
                .where(
                    BotInstance.id == bot_instance_id,
                    BotInstance.tenant_id == tenant_id,
                    BotInstance.deleted_at.is_(None),
                )
                .values(
                    processed_updates=BotInstance.processed_updates + 1,
                    last_update_at=datetime.now(UTC),
                )
            )
            if changed.rowcount != 1:
                raise LookupError("active bot instance not found in tenant")

        await self.transactions.run(write)

    async def record(
        self,
        *,
        tenant_id: str,
        bot_instance_id: str,
        event_name: str,
        telegram_user_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        count_update: bool = False,
    ) -> str:
        safe_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if not any(word in key.lower() for word in ("token", "secret", "password"))
        }

        async def write(session: AsyncSession) -> str:
            values: dict[str, Any] = {"last_update_at": datetime.now(UTC)}
            if count_update:
                values["processed_updates"] = BotInstance.processed_updates + 1
            if event_name in BUTTON_EVENTS:
                values["button_clicks"] = BotInstance.button_clicks + 1
            changed = await session.execute(
                update(BotInstance)
                .where(
                    BotInstance.id == bot_instance_id,
                    BotInstance.tenant_id == tenant_id,
                    BotInstance.deleted_at.is_(None),
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise LookupError("active bot instance not found in tenant")
            event = ProductEvent(
                tenant_id=tenant_id,
                bot_instance_id=bot_instance_id,
                telegram_user_id=telegram_user_id,
                event_name=event_name,
                occurred_at=datetime.now(UTC),
                metadata_json=safe_metadata,
            )
            session.add(event)
            await session.flush()
            return event.id

        return await self.transactions.run(write)

    async def stats(self, bot_instance_id: str, tenant_id: str) -> BotEventStats:
        async with self.session_factory() as session:
            scope = (
                ProductEvent.bot_instance_id == bot_instance_id,
                ProductEvent.tenant_id == tenant_id,
            )
            unique_users = await session.scalar(
                select(func.count(distinct(ProductEvent.telegram_user_id))).where(*scope)
            )
            total = await session.scalar(select(func.count(ProductEvent.id)).where(*scope))
            recent = await session.scalar(
                select(func.count(ProductEvent.id)).where(
                    *scope, ProductEvent.occurred_at >= datetime.now(UTC) - timedelta(hours=24)
                )
            )
            last = await session.scalar(
                select(ProductEvent)
                .where(*scope)
                .order_by(ProductEvent.occurred_at.desc())
                .limit(1)
            )
            popular = list(
                (
                    await session.execute(
                        select(ProductEvent.event_name, func.count(ProductEvent.id).label("count"))
                        .where(*scope, ProductEvent.event_name.in_(BUTTON_EVENTS))
                        .group_by(ProductEvent.event_name)
                        .order_by(func.count(ProductEvent.id).desc(), ProductEvent.event_name.asc())
                        .limit(5)
                    )
                ).all()
            )
            return BotEventStats(
                unique_users=int(unique_users or 0),
                total_events=int(total or 0),
                events_last_24h=int(recent or 0),
                last_event_name=last.event_name if last else None,
                last_event_at=last.occurred_at if last else None,
                popular_buttons=[(str(name), int(count)) for name, count in popular],
            )
