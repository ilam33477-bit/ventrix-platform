from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..models import TelegramRuntimeLease


@dataclass(frozen=True, slots=True)
class RuntimeOwnership:
    connection_id: str
    owner_instance_id: str
    generation: int
    lease_until: datetime


class TelegramRuntimeLeaseStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int = 45,
    ) -> None:
        self.transactions = SQLiteTransactionManager(session_factory)
        self.ttl = timedelta(seconds=max(30, ttl_seconds))

    async def acquire(self, connection_id: str, owner_instance_id: str) -> RuntimeOwnership | None:
        now = datetime.now(UTC)
        lease_until = now + self.ttl

        async def write(session: AsyncSession) -> RuntimeOwnership | None:
            table = TelegramRuntimeLease.__table__
            statement = insert(table).values(
                connection_id=connection_id,
                owner_instance_id=owner_instance_id,
                generation=1,
                lease_until=lease_until,
                heartbeat_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[table.c.connection_id],
                set_={
                    "owner_instance_id": owner_instance_id,
                    "generation": case(
                        (table.c.owner_instance_id == owner_instance_id, table.c.generation),
                        else_=table.c.generation + 1,
                    ),
                    "lease_until": lease_until,
                    "heartbeat_at": now,
                    "updated_at": now,
                },
                where=(table.c.lease_until <= now)
                | (table.c.owner_instance_id == owner_instance_id),
            )
            result = await session.execute(statement)
            if result.rowcount != 1:
                return None
            row = await session.get(TelegramRuntimeLease, connection_id)
            return RuntimeOwnership(
                connection_id, row.owner_instance_id, row.generation, row.lease_until
            )

        return await self.transactions.run(write)

    async def heartbeat(self, ownership: RuntimeOwnership) -> RuntimeOwnership | None:
        now = datetime.now(UTC)
        lease_until = now + self.ttl

        async def write(session: AsyncSession) -> RuntimeOwnership | None:
            row = await session.scalar(
                select(TelegramRuntimeLease).where(
                    TelegramRuntimeLease.connection_id == ownership.connection_id,
                    TelegramRuntimeLease.owner_instance_id == ownership.owner_instance_id,
                    TelegramRuntimeLease.generation == ownership.generation,
                )
            )
            if row is None:
                return None
            row.heartbeat_at = now
            row.lease_until = lease_until
            return RuntimeOwnership(
                row.connection_id, row.owner_instance_id, row.generation, lease_until
            )

        return await self.transactions.run(write)

    async def is_current(self, ownership: RuntimeOwnership) -> bool:
        async with self.transactions.session_factory() as session:
            return bool(
                await session.scalar(
                    select(TelegramRuntimeLease.connection_id).where(
                        TelegramRuntimeLease.connection_id == ownership.connection_id,
                        TelegramRuntimeLease.owner_instance_id == ownership.owner_instance_id,
                        TelegramRuntimeLease.generation == ownership.generation,
                        TelegramRuntimeLease.lease_until > datetime.now(UTC),
                    )
                )
            )

    async def release(self, ownership: RuntimeOwnership) -> bool:
        async def write(session: AsyncSession) -> bool:
            row = await session.scalar(
                select(TelegramRuntimeLease).where(
                    TelegramRuntimeLease.connection_id == ownership.connection_id,
                    TelegramRuntimeLease.owner_instance_id == ownership.owner_instance_id,
                    TelegramRuntimeLease.generation == ownership.generation,
                )
            )
            if row is None:
                return False
            await session.delete(row)
            return True

        return await self.transactions.run(write)
