from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JOB_PRIORITY, SQLiteJobQueue
from ..models import (
    BackgroundJob,
    ProblemTransition,
    RuntimeHealth,
    TelegramConnection,
    Tenant,
    TenantAIFeedbackProfile,
    TenantAnalysisSchedule,
    TenantSettings,
)
from ..services.product_events import add_system_event
from ..timezones import normalize_timezone, timezone_info

logger = logging.getLogger(__name__)


def next_analysis_time(
    *,
    now: datetime,
    timezone: str,
    report_time: time,
    enabled_days: list[int],
    advance_minutes: int,
) -> datetime:
    normalized_timezone = normalize_timezone(timezone)
    zone = timezone_info(normalized_timezone)
    local_now = now.astimezone(zone)
    enabled = set(enabled_days)
    if not enabled or not enabled <= set(range(7)):
        raise ValueError("enabled_days must contain weekdays 0..6")
    for offset in range(8):
        local_date = local_now.date() + timedelta(days=offset)
        if local_date.weekday() not in enabled:
            continue
        report_local = datetime.combine(local_date, report_time, zone)
        candidate = report_local - timedelta(minutes=advance_minutes)
        if candidate > local_now:
            return candidate.astimezone(UTC)
    raise RuntimeError("could not calculate next analysis time")


class TenantAnalysisScheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue | None = None,
        incremental_interval_seconds: int = 30,
        reconciliation_interval_seconds: int = 3600,
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.queue = queue or SQLiteJobQueue(session_factory)
        self.incremental_interval_seconds = incremental_interval_seconds
        self.reconciliation_interval_seconds = reconciliation_interval_seconds

    async def ensure_schedules(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)

        async def write(session: AsyncSession) -> int:
            tenants = list(await session.scalars(select(Tenant).where(Tenant.deleted_at.is_(None))))
            existing = set(await session.scalars(select(TenantAnalysisSchedule.tenant_id)))
            created = 0
            for tenant in tenants:
                settings = await session.scalar(
                    select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
                )
                if settings is None:
                    continue
                try:
                    normalized_timezone = normalize_timezone(settings.timezone)
                    if settings.timezone != normalized_timezone:
                        settings.timezone = normalized_timezone
                    expires = None
                    if tenant.subscription_expires_at:
                        expires = datetime.combine(
                            tenant.subscription_expires_at,
                            time.max,
                            timezone_info(normalized_timezone),
                        ).astimezone(UTC)
                    if tenant.id in existing:
                        schedule = await session.scalar(
                            select(TenantAnalysisSchedule).where(
                                TenantAnalysisSchedule.tenant_id == tenant.id
                            )
                        )
                        if schedule is not None:
                            try:
                                schedule_timezone = normalize_timezone(schedule.timezone)
                            except ValueError:
                                schedule_timezone = normalized_timezone
                            if schedule.timezone != schedule_timezone:
                                schedule.timezone = schedule_timezone
                            schedule.access_expires_at = expires
                            schedule.access_status = (
                                "active" if tenant.status == "active" else tenant.status
                            )
                            schedule.analysis_enabled = settings.analysis_enabled
                            schedule.report_time = settings.daily_report_time
                            schedule.enabled_days = list(settings.enabled_days)
                            schedule.history_window_days = settings.history_window_days
                            schedule.advance_minutes = settings.analysis_advance_minutes
                            if schedule.next_analysis_at is None:
                                schedule.next_analysis_at = next_analysis_time(
                                    now=current,
                                    timezone=schedule.timezone,
                                    report_time=schedule.report_time,
                                    enabled_days=list(schedule.enabled_days),
                                    advance_minutes=schedule.advance_minutes,
                                )
                        continue
                    schedule = TenantAnalysisSchedule(
                        tenant_id=tenant.id,
                        timezone=normalized_timezone,
                        report_time=settings.daily_report_time,
                        enabled_days=list(settings.enabled_days),
                        history_window_days=settings.history_window_days,
                        advance_minutes=settings.analysis_advance_minutes,
                        analysis_enabled=settings.analysis_enabled,
                        next_analysis_at=next_analysis_time(
                            now=current,
                            timezone=normalized_timezone,
                            report_time=settings.daily_report_time,
                            enabled_days=list(settings.enabled_days),
                            advance_minutes=settings.analysis_advance_minutes,
                        ),
                        access_started_at=tenant.created_at,
                        access_expires_at=expires,
                        access_status="active" if tenant.status == "active" else tenant.status,
                    )
                    session.add(schedule)
                    created += 1
                except ValueError as exc:
                    logger.error(
                        "Skipping tenant with invalid schedule timezone",
                        extra={"tenant_id": tenant.id, "timezone": settings.timezone},
                        exc_info=exc,
                    )
            return created

        return await self.transactions.run(write)

    async def tick(self, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        await self.ensure_schedules(current)
        operational_jobs = await self._schedule_operational_jobs(current)
        async with self.session_factory() as session:
            schedules = list(
                await session.scalars(
                    select(TenantAnalysisSchedule).where(
                        TenantAnalysisSchedule.analysis_enabled.is_(True),
                        TenantAnalysisSchedule.next_analysis_at.is_not(None),
                        TenantAnalysisSchedule.next_analysis_at <= current,
                    )
                )
            )
        job_ids: list[str] = list(operational_jobs)
        for schedule in schedules:
            try:
                normalize_timezone(schedule.timezone)
                tenant = await self._eligible_tenant(schedule, current)
                if tenant is None:
                    continue
                planned_for = schedule.next_analysis_at
                report_due_at = planned_for + timedelta(minutes=schedule.advance_minutes)
                correlation_id = str(uuid4())
                job_id = await self.queue.enqueue(
                    "analysis.pipeline",
                    {
                        "report_due_at": report_due_at.isoformat(),
                        "history_window_days": schedule.history_window_days,
                        "trigger": "scheduled",
                    },
                    tenant_id=schedule.tenant_id,
                    priority=50,
                    idempotency_key=f"scheduled-analysis:{schedule.tenant_id}:{report_due_at.isoformat()}",
                    correlation_id=correlation_id,
                    is_heavy=True,
                    category="analysis",
                    max_attempts=5,
                )
                job_ids.append(job_id)
                await self._record_event(
                    schedule.tenant_id,
                    "scheduled_analysis_created",
                    {"job_id": job_id, "report_due_at": report_due_at.isoformat()},
                )
                await self._advance(schedule.id, planned_for, current)
            except ValueError as exc:
                logger.error(
                    "Skipping invalid tenant schedule",
                    extra={"tenant_id": schedule.tenant_id, "timezone": schedule.timezone},
                    exc_info=exc,
                )
        await self._heartbeat(current, len(job_ids))
        return job_ids

    async def _schedule_operational_jobs(self, now: datetime) -> list[str]:
        async with self.session_factory() as session:
            connections = list(
                await session.scalars(
                    select(TelegramConnection)
                    .join(Tenant, Tenant.id == TelegramConnection.tenant_id)
                    .join(
                        TenantAnalysisSchedule,
                        TenantAnalysisSchedule.tenant_id == TelegramConnection.tenant_id,
                    )
                    .where(
                        Tenant.status == "active",
                        Tenant.deleted_at.is_(None),
                        TenantAnalysisSchedule.access_status == "active",
                        (
                            TenantAnalysisSchedule.access_expires_at.is_(None)
                            | (TenantAnalysisSchedule.access_expires_at >= now)
                        ),
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.session_secret_id.is_not(None),
                        TelegramConnection.status.in_(("connected", "ready")),
                    )
                )
            )
            tenant_ids = sorted({item.tenant_id for item in connections})
            feedback_tenant_ids = list(
                await session.scalars(
                    select(Tenant.id)
                    .join(
                        TenantAnalysisSchedule,
                        TenantAnalysisSchedule.tenant_id == Tenant.id,
                    )
                    .where(
                        Tenant.status == "active",
                        Tenant.deleted_at.is_(None),
                        TenantAnalysisSchedule.access_status == "active",
                        (
                            TenantAnalysisSchedule.access_expires_at.is_(None)
                            | (TenantAnalysisSchedule.access_expires_at >= now)
                        ),
                    )
                )
            )
            feedback_due: list[tuple[str, datetime]] = []
            for tenant_id in feedback_tenant_ids:
                profile = await session.scalar(
                    select(TenantAIFeedbackProfile).where(
                        TenantAIFeedbackProfile.tenant_id == tenant_id
                    )
                )
                pending_query = select(
                    func.count(ProblemTransition.id),
                    func.min(ProblemTransition.occurred_at),
                    func.max(ProblemTransition.occurred_at),
                ).where(
                    ProblemTransition.tenant_id == tenant_id,
                    ProblemTransition.to_status == "false_positive",
                )
                if profile and profile.last_processed_transition_at:
                    pending_query = pending_query.where(
                        ProblemTransition.occurred_at > profile.last_processed_transition_at
                    )
                count, oldest, latest = (await session.execute(pending_query)).one()
                weekly_due = oldest is not None and oldest <= now - timedelta(days=7)
                if count and latest is not None and (count >= 10 or weekly_due):
                    feedback_due.append((tenant_id, latest))
        fetch_bucket = int(now.timestamp() // self.incremental_interval_seconds)
        reconcile_bucket = int(now.timestamp() // self.reconciliation_interval_seconds)
        job_ids: list[str] = []
        for connection in connections:
            job_ids.append(
                await self.queue.enqueue(
                    "telegram.catch_up",
                    {},
                    tenant_id=connection.tenant_id,
                    telegram_account_id=connection.id,
                    priority=JOB_PRIORITY["P1"],
                    idempotency_key=f"telegram-fetch:{connection.id}:bucket:{fetch_bucket}",
                    correlation_id=str(uuid4()),
                    is_heavy=False,
                    category="telegram_rpc",
                    cost_class="light",
                    max_attempts=8,
                )
            )
        for tenant_id in tenant_ids:
            job_ids.append(
                await self.queue.enqueue(
                    "analysis.hourly",
                    {},
                    tenant_id=tenant_id,
                    priority=JOB_PRIORITY["P3"],
                    idempotency_key=f"hourly-reconciliation:{tenant_id}:{reconcile_bucket}",
                    correlation_id=str(uuid4()),
                    is_heavy=False,
                    category="reconciliation",
                    cost_class="light",
                    max_attempts=3,
                )
            )
        for tenant_id, latest in feedback_due:
            feedback_week = int(now.timestamp() // (7 * 24 * 3600))
            job_ids.append(
                await self.queue.enqueue(
                    "feedback.synthesize",
                    {},
                    tenant_id=tenant_id,
                    priority=JOB_PRIORITY["P3"],
                    idempotency_key=(
                        f"feedback-learning:{tenant_id}:{latest.isoformat()}:{feedback_week}"
                    ),
                    correlation_id=str(uuid4()),
                    is_heavy=False,
                    category="ai",
                    cost_class="standard",
                    max_attempts=3,
                )
            )
        return job_ids

    async def _eligible_tenant(
        self, schedule: TenantAnalysisSchedule, now: datetime
    ) -> Tenant | None:
        async with self.session_factory() as session:
            tenant = await session.get(Tenant, schedule.tenant_id)
        if tenant is None or tenant.deleted_at is not None or tenant.status != "active":
            return None
        expires = schedule.access_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires is not None and expires < now:

            async def expire(session: AsyncSession) -> None:
                current = await session.get(TenantAnalysisSchedule, schedule.id)
                current.access_status = "expired"
                await add_system_event(
                    session,
                    tenant_id=schedule.tenant_id,
                    event_name="access_expired",
                )

            await self.transactions.run(expire)
            return None
        return tenant

    async def _advance(self, schedule_id: str, planned_for: datetime, now: datetime) -> None:
        async def write(session: AsyncSession) -> None:
            schedule = await session.get(TenantAnalysisSchedule, schedule_id)
            schedule.last_enqueued_for = planned_for
            schedule.next_analysis_at = next_analysis_time(
                now=now + timedelta(seconds=1),
                timezone=schedule.timezone,
                report_time=schedule.report_time,
                enabled_days=list(schedule.enabled_days),
                advance_minutes=schedule.advance_minutes,
            )

        await self.transactions.run(write)

    async def _record_event(
        self, tenant_id: str, event_name: str, metadata: dict[str, str]
    ) -> None:
        async def write(session: AsyncSession) -> None:
            await add_system_event(
                session,
                tenant_id=tenant_id,
                event_name=event_name,
                metadata=metadata,
            )

        await self.transactions.run(write)

    async def _heartbeat(self, now: datetime, enqueued: int) -> None:
        async def write(session: AsyncSession) -> None:
            health = await session.scalar(
                select(RuntimeHealth).where(RuntimeHealth.component == "scheduler")
            )
            if health is None:
                session.add(
                    RuntimeHealth(
                        component="scheduler",
                        status="healthy",
                        heartbeat_at=now,
                        details_json={"last_enqueued": enqueued},
                    )
                )
            else:
                health.status = "healthy"
                health.heartbeat_at = now
                health.details_json = {"last_enqueued": enqueued}

        await self.transactions.run(write)

    async def trigger_now(self, tenant_id: str) -> str:
        async with self.session_factory() as session:
            active = await session.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.tenant_id == tenant_id,
                    BackgroundJob.is_heavy.is_(True),
                    BackgroundJob.status.in_(
                        ("pending", "scheduled", "waiting", "retry", "running")
                    ),
                )
            )
            if active:
                return active.id
        now = datetime.now(UTC)
        return await self.queue.enqueue(
            "analysis.pipeline",
            {"report_due_at": now.isoformat(), "trigger": "manual"},
            tenant_id=tenant_id,
            priority=10,
            idempotency_key=f"manual-analysis:{tenant_id}:{now:%Y%m%d%H%M}",
            correlation_id=str(uuid4()),
            is_heavy=True,
            category="ai_heavy",
            cost_class="heavy",
            max_attempts=5,
        )
