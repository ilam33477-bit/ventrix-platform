from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import AI_PROVIDER_COST_CLASSES, AI_PROVIDER_JOB_TYPES
from ..models import BackgroundJob


class JSONProvider(Protocol):
    async def generate_json(self, **kwargs: Any) -> tuple[str, dict[str, int]]: ...


class AIConcurrencyBusy(RuntimeError):
    retry_after_seconds = 2
    error_code = "ai_capacity_busy"

    def __init__(self) -> None:
        super().__init__("AI временно занят. Повторите попытку через несколько секунд.")


class SharedAIProvider:
    """Counts interactive provider calls in the same DB-backed budget as worker jobs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: JSONProvider,
        *,
        max_active_requests: int,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.max_active_requests = max_active_requests
        self.heartbeat_seconds = heartbeat_seconds
        self.transactions = SQLiteTransactionManager(session_factory)

    async def generate_json(self, **kwargs: Any) -> tuple[str, dict[str, int]]:
        lease_id, owner = await self._acquire()
        heartbeat = asyncio.create_task(self._heartbeat(lease_id, owner))
        try:
            result = await self.provider.generate_json(**kwargs)
        except BaseException as exc:
            await self._release(lease_id, owner, error_code=getattr(exc, "error_code", type(exc).__name__))
            raise
        else:
            await self._release(lease_id, owner)
            return result
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _acquire(self) -> tuple[str, str]:
        owner = f"direct-ai:{uuid4()}"
        now = datetime.now(UTC)

        async def write(session: AsyncSession) -> str | None:
            active = int(
                await session.scalar(
                    select(func.count(BackgroundJob.id)).where(
                        BackgroundJob.status == "running",
                        or_(
                            BackgroundJob.cost_class.in_(AI_PROVIDER_COST_CLASSES),
                            BackgroundJob.job_type.in_(AI_PROVIDER_JOB_TYPES),
                        ),
                    )
                )
                or 0
            )
            if active >= self.max_active_requests:
                return None
            lease = BackgroundJob(
                job_type="ai.interactive",
                category="ai_interactive",
                cost_class="ai_fast",
                payload_json={},
                status="running",
                priority=0,
                scheduled_at=now,
                started_at=now,
                locked_at=now,
                heartbeat_at=now,
                locked_by=owner,
                max_attempts=1,
            )
            session.add(lease)
            await session.flush()
            return lease.id

        lease_id = await self.transactions.run(write)
        if lease_id is None:
            raise AIConcurrencyBusy()
        return lease_id, owner

    async def _heartbeat(self, lease_id: str, owner: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)

            async def write(session: AsyncSession) -> bool:
                lease = await session.get(BackgroundJob, lease_id)
                if lease is None or lease.status != "running" or lease.locked_by != owner:
                    return False
                lease.locked_at = datetime.now(UTC)
                lease.heartbeat_at = lease.locked_at
                return True

            if not await self.transactions.run(write):
                return

    async def _release(
        self, lease_id: str, owner: str, *, error_code: str | None = None
    ) -> None:
        async def write(session: AsyncSession) -> None:
            lease = await session.get(BackgroundJob, lease_id)
            if lease is None or lease.status != "running" or lease.locked_by != owner:
                return
            lease.status = "failed" if error_code else "completed"
            lease.finished_at = datetime.now(UTC)
            lease.locked_by = None
            lease.locked_at = None
            lease.heartbeat_at = None
            lease.last_error = f"{error_code}: execution failed" if error_code else None

        await self.transactions.run(write)
