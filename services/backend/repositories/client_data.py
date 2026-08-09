from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    AnalysisRun,
    BackgroundJob,
    OperationalProblem,
    Report,
    TelegramConnection,
    TenantAnalysisSchedule,
    TenantDailyMetric,
)


class TenantClientRepository:
    """Read/write boundary that always applies one immutable tenant scope."""

    def __init__(self, session: AsyncSession, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def current_connection(self) -> TelegramConnection | None:
        return await self.session.scalar(
            select(TelegramConnection)
            .where(
                TelegramConnection.tenant_id == self.tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            .order_by(TelegramConnection.created_at.desc())
            .limit(1)
        )

    async def current_analysis(self) -> AnalysisRun | None:
        return await self.session.scalar(
            select(AnalysisRun)
            .where(AnalysisRun.tenant_id == self.tenant_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(1)
        )

    async def current_job(self) -> BackgroundJob | None:
        return await self.session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.tenant_id == self.tenant_id,
                BackgroundJob.status.in_(("pending", "scheduled", "waiting", "retry", "running")),
            )
            .order_by(BackgroundJob.created_at.desc())
            .limit(1)
        )

    async def job(self, job_id: str) -> BackgroundJob | None:
        return await self.session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.id == job_id,
                BackgroundJob.tenant_id == self.tenant_id,
            )
        )

    async def problems(self) -> list[OperationalProblem]:
        return list(
            await self.session.scalars(
                select(OperationalProblem)
                .where(OperationalProblem.tenant_id == self.tenant_id)
                .order_by(OperationalProblem.occurred_at.desc())
            )
        )

    async def problem(self, problem_id: str) -> OperationalProblem | None:
        return await self.session.scalar(
            select(OperationalProblem).where(
                OperationalProblem.id == problem_id,
                OperationalProblem.tenant_id == self.tenant_id,
            )
        )

    async def reports(self) -> list[Report]:
        return list(
            await self.session.scalars(
                select(Report)
                .join(AnalysisRun, AnalysisRun.id == Report.analysis_run_id)
                .where(Report.tenant_id == self.tenant_id)
                .where(AnalysisRun.trigger != "signal_escalation")
                .order_by(Report.period_end.desc())
            )
        )

    async def report(self, report_id: str) -> Report | None:
        return await self.session.scalar(
            select(Report).where(
                Report.id == report_id,
                Report.tenant_id == self.tenant_id,
            )
        )

    async def schedule(self) -> TenantAnalysisSchedule | None:
        return await self.session.scalar(
            select(TenantAnalysisSchedule).where(TenantAnalysisSchedule.tenant_id == self.tenant_id)
        )

    async def latest_metrics(self) -> TenantDailyMetric | None:
        return await self.session.scalar(
            select(TenantDailyMetric)
            .where(TenantDailyMetric.tenant_id == self.tenant_id)
            .order_by(TenantDailyMetric.metric_date.desc())
            .limit(1)
        )
