from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from ..database import SQLiteTransactionManager
from ..models import BackgroundJob, TenantQueueState

AVAILABLE_STATUSES = ("pending", "scheduled", "waiting", "retry", "retry_scheduled")
RESERVED_LANE_CATEGORIES = frozenset({"critical", "notification", "realtime", "telegram_rpc"})
HEAVY_JOB_TYPES = {
    "telegram_initial_sync",
    "telegram_incremental_sync",
    "message_preprocessing",
    "ai_batch_analysis",
    "problem_deduplication",
    "report_generation",
    "analysis.pipeline",
    "analysis.deep",
    "report.employee",
    "report.client",
    "report.company",
}

JOB_PRIORITY = {
    "P0": 0,
    "P1": 10,
    "P2": 20,
    "P3": 30,
    "P4": 40,
    "P5": 50,
    "P6": 60,
}


class JobDeferred(RuntimeError):
    def __init__(self, delay_seconds: int, reason: str) -> None:
        super().__init__("background job deferred")
        self.delay_seconds = max(1, delay_seconds)
        self.reason = reason[:200]


@dataclass(frozen=True, slots=True)
class JobLease:
    id: str
    tenant_id: str | None
    telegram_account_id: str | None
    dialog_id: str | None
    correlation_id: str | None
    job_type: str
    category: str
    cost_class: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    locked_by: str


class SQLiteJobQueue:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_active_tenant_jobs: int = 2,
        tenant_max_active_heavy_jobs: int = 1,
        category_limits: dict[str, int] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.max_active_tenant_jobs = max_active_tenant_jobs
        self.tenant_max_active_heavy_jobs = tenant_max_active_heavy_jobs
        self.category_limits = category_limits or {
            "ai": 2,
            "telegram": 1,
            "sync": 1,
            "report": 1,
        }

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        tenant_id: str | None = None,
        priority: int = 100,
        scheduled_at: datetime | None = None,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        telegram_account_id: str | None = None,
        dialog_id: str | None = None,
        correlation_id: str | None = None,
        is_heavy: bool | None = None,
        category: str = "general",
        cost_class: str = "light",
        partition_key: str | None = None,
        partition_sequence: int | None = None,
    ) -> str:
        if not job_type.strip():
            raise ValueError("job_type is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")

        async def write(session: AsyncSession) -> str:
            if idempotency_key:
                existing = await session.scalar(
                    select(BackgroundJob.id).where(BackgroundJob.idempotency_key == idempotency_key)
                )
                if existing:
                    return existing
            run_at = scheduled_at or datetime.now(UTC)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
            job = BackgroundJob(
                tenant_id=tenant_id,
                telegram_account_id=telegram_account_id,
                dialog_id=dialog_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                job_type=job_type.strip(),
                payload_json=dict(payload),
                status="scheduled" if run_at > datetime.now(UTC) else "pending",
                priority=priority,
                scheduled_at=run_at,
                attempts=0,
                max_attempts=max_attempts,
                is_heavy=job_type in HEAVY_JOB_TYPES if is_heavy is None else is_heavy,
                category=category,
                cost_class=cost_class,
                partition_key=partition_key,
                partition_sequence=partition_sequence,
            )
            session.add(job)
            await session.flush()
            return job.id

        return await self.transactions.run(write)

    async def claim_next(
        self,
        worker_id: str,
        *,
        allowed_categories: frozenset[str] | None = None,
        telegram_account_id: str | None = None,
    ) -> JobLease | None:
        now = datetime.now(UTC)

        async def write(session: AsyncSession) -> JobLease | None:
            filters = [
                BackgroundJob.status.in_(AVAILABLE_STATUSES),
                BackgroundJob.scheduled_at <= now,
            ]
            if allowed_categories is not None:
                if not allowed_categories:
                    return None
                filters.append(BackgroundJob.category.in_(allowed_categories))
            if telegram_account_id is not None:
                filters.append(BackgroundJob.telegram_account_id == telegram_account_id)
            predecessor = aliased(BackgroundJob)
            running_partition = aliased(BackgroundJob)
            filters.append(
                or_(
                    BackgroundJob.partition_key.is_(None),
                    (
                        ~exists(
                            select(running_partition.id).where(
                                running_partition.partition_key == BackgroundJob.partition_key,
                                running_partition.status == "running",
                            )
                        )
                        & ~exists(
                            select(predecessor.id).where(
                                predecessor.partition_key == BackgroundJob.partition_key,
                                predecessor.status.in_((*AVAILABLE_STATUSES, "running")),
                                predecessor.partition_sequence.is_not(None),
                                predecessor.partition_sequence < BackgroundJob.partition_sequence,
                            )
                        )
                    ),
                )
            )
            candidates = list(
                await session.scalars(
                    select(BackgroundJob)
                    .where(*filters)
                    .order_by(
                        BackgroundJob.priority.asc(),
                        BackgroundJob.scheduled_at.asc(),
                        BackgroundJob.created_at.asc(),
                    )
                    .limit(100)
                )
            )
            if not candidates:
                return None
            tenant_ids = {item.tenant_id for item in candidates if item.tenant_id}
            states = {
                item.tenant_id: item
                for item in await session.scalars(
                    select(TenantQueueState).where(TenantQueueState.tenant_id.in_(tenant_ids))
                )
            }
            running_by_tenant = dict(
                (
                    await session.execute(
                        select(BackgroundJob.tenant_id, func.count(BackgroundJob.id))
                        .where(
                            BackgroundJob.status == "running",
                            BackgroundJob.tenant_id.in_(tenant_ids),
                        )
                        .group_by(BackgroundJob.tenant_id)
                    )
                ).all()
            )
            heavy_by_tenant = dict(
                (
                    await session.execute(
                        select(BackgroundJob.tenant_id, func.count(BackgroundJob.id))
                        .where(
                            BackgroundJob.status == "running",
                            BackgroundJob.is_heavy.is_(True),
                            BackgroundJob.tenant_id.in_(tenant_ids),
                        )
                        .group_by(BackgroundJob.tenant_id)
                    )
                ).all()
            )
            category_counts = dict(
                (
                    await session.execute(
                        select(BackgroundJob.category, func.count(BackgroundJob.id))
                        .where(BackgroundJob.status == "running")
                        .group_by(BackgroundJob.category)
                    )
                ).all()
            )
            eligible = []
            for candidate in candidates:
                if candidate.tenant_id:
                    if (
                        candidate.category not in RESERVED_LANE_CATEGORIES
                        and running_by_tenant.get(candidate.tenant_id, 0)
                        >= self.max_active_tenant_jobs
                    ):
                        continue
                    if (
                        candidate.is_heavy
                        and heavy_by_tenant.get(candidate.tenant_id, 0)
                        >= self.tenant_max_active_heavy_jobs
                    ):
                        continue
                category_limit = self.category_limits.get(candidate.category)
                if (
                    category_limit is not None
                    and category_counts.get(candidate.category, 0) >= category_limit
                ):
                    continue
                eligible.append(candidate)
            if not eligible:
                return None
            epoch = datetime.min.replace(tzinfo=UTC)

            def database_time(value: datetime | None) -> datetime:
                if value is None:
                    return epoch
                return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

            job = min(
                eligible,
                key=lambda item: (
                    item.priority,
                    database_time(states[item.tenant_id].last_claimed_at)
                    if item.tenant_id in states
                    else epoch,
                    database_time(item.scheduled_at),
                    database_time(item.created_at),
                ),
            )
            claimed = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == job.id,
                    BackgroundJob.status.in_(AVAILABLE_STATUSES),
                )
                .values(
                    status="running",
                    started_at=now,
                    locked_at=now,
                    heartbeat_at=now,
                    locked_by=worker_id,
                    last_error=None,
                )
            )
            if claimed.rowcount != 1:
                return None
            if job.tenant_id:
                state = states.get(job.tenant_id)
                if state is None:
                    session.add(TenantQueueState(tenant_id=job.tenant_id, last_claimed_at=now))
                else:
                    state.last_claimed_at = now
            return JobLease(
                id=job.id,
                tenant_id=job.tenant_id,
                telegram_account_id=job.telegram_account_id,
                dialog_id=job.dialog_id,
                correlation_id=job.correlation_id,
                job_type=job.job_type,
                category=job.category,
                cost_class=job.cost_class,
                payload=dict(job.payload_json),
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                locked_by=worker_id,
            )

        return await self.transactions.run(write)

    async def complete(self, lease: JobLease, result: dict[str, Any] | None = None) -> bool:
        async def write(session: AsyncSession) -> bool:
            job = await session.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.id == lease.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by == lease.locked_by,
                )
            )
            if job is None:
                return False
            job.status = "completed"
            job.result_json = dict(result or {})
            job.finished_at = datetime.now(UTC)
            job.locked_by = None
            job.locked_at = None
            job.heartbeat_at = None
            return True

        return await self.transactions.run(write)

    async def heartbeat(self, lease: JobLease) -> bool:
        async def write(session: AsyncSession) -> bool:
            refreshed = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == lease.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by == lease.locked_by,
                )
                .values(locked_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC))
            )
            return refreshed.rowcount == 1

        return await self.transactions.run(write)

    async def fail(self, lease: JobLease, error: BaseException) -> str:
        async def write(session: AsyncSession) -> str:
            job = await session.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.id == lease.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by == lease.locked_by,
                )
            )
            if job is None:
                return "lease_lost"
            job.attempts += 1
            job.last_error = f"{type(error).__name__}: execution failed"
            job.locked_by = None
            job.locked_at = None
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
            else:
                job.status = "retry_scheduled"
                requested_delay = getattr(error, "retry_after_seconds", None)
                delay = (
                    min(86_400, int(requested_delay))
                    if requested_delay is not None
                    else min(300, 2 ** max(0, job.attempts - 1))
                )
                job.scheduled_at = datetime.now(UTC) + timedelta(seconds=delay)
            return job.status

        return await self.transactions.run(write)

    async def recover_stale(self, lock_timeout: timedelta) -> int:
        cutoff = datetime.now(UTC) - lock_timeout

        async def write(session: AsyncSession) -> int:
            jobs = list(
                await session.scalars(
                    select(BackgroundJob).where(
                        BackgroundJob.status == "running",
                        BackgroundJob.locked_at.is_not(None),
                        BackgroundJob.locked_at < cutoff,
                    )
                )
            )
            now = datetime.now(UTC)
            for job in jobs:
                job.attempts += 1
                job.last_error = "WorkerRestart: stale lease recovered"
                job.locked_by = None
                job.locked_at = None
                if job.attempts >= job.max_attempts:
                    job.status = "failed"
                    job.finished_at = now
                else:
                    job.status = "retry_scheduled"
                    job.scheduled_at = now
            return len(jobs)

        return await self.transactions.run(write)

    async def get(self, job_id: str) -> BackgroundJob | None:
        async with self.session_factory() as session:
            return await session.get(BackgroundJob, job_id)

    async def cancel(self, job_id: str, *, tenant_id: str | None = None) -> bool:
        async def write(session: AsyncSession) -> bool:
            filters = [
                BackgroundJob.id == job_id,
                BackgroundJob.status.in_((*AVAILABLE_STATUSES, "waiting", "running")),
            ]
            if tenant_id is not None:
                filters.append(BackgroundJob.tenant_id == tenant_id)
            changed = await session.execute(
                update(BackgroundJob)
                .where(*filters)
                .values(
                    status="cancelled",
                    finished_at=datetime.now(UTC),
                    locked_by=None,
                    locked_at=None,
                    heartbeat_at=None,
                )
            )
            return changed.rowcount == 1

        return await self.transactions.run(write)

    async def update_progress(self, lease: JobLease, progress: dict[str, Any]) -> bool:
        async def write(session: AsyncSession) -> bool:
            changed = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == lease.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by == lease.locked_by,
                )
                .values(progress_json=dict(progress), heartbeat_at=datetime.now(UTC))
            )
            return changed.rowcount == 1

        return await self.transactions.run(write)

    async def defer(self, lease: JobLease, delay_seconds: int, reason: str) -> bool:
        async def write(session: AsyncSession) -> bool:
            changed = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == lease.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by == lease.locked_by,
                )
                .values(
                    status="waiting",
                    scheduled_at=datetime.now(UTC) + timedelta(seconds=max(1, delay_seconds)),
                    delay_reason=reason[:200],
                    locked_by=None,
                    locked_at=None,
                    heartbeat_at=None,
                )
            )
            return changed.rowcount == 1

        return await self.transactions.run(write)
