from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..analysis.service import AnalysisPipelineService
from ..bot.sqlite_storage import SQLiteFSMStorage
from ..database import SQLiteTransactionManager
from ..models import BackgroundJob, OperationalProblem, Report, TelegramDialog
from ..telegram_sessions.service import TelegramConnectionService
from .queue import JobLease


class MaintenanceJobHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        analysis: AnalysisPipelineService,
        *,
        connection_service: TelegramConnectionService | None,
        fsm_ttl_hours: int,
    ) -> None:
        self.session_factory = session_factory
        self.analysis = analysis
        self.connection_service = connection_service
        self.transactions = SQLiteTransactionManager(session_factory)
        self.storage = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=fsm_ttl_hours))

    async def telegram_sync(self, job: JobLease) -> dict[str, object]:
        if job.tenant_id is None or self.connection_service is None:
            raise RuntimeError("Telegram ingestion is not configured")
        run = await self.connection_service.start_initial_sync(job.tenant_id)
        return {"sync_run_id": run.id, "status": run.status}

    async def dialog_classification(self, job: JobLease) -> dict[str, int]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        async with self.session_factory() as session:
            selected = await session.scalar(
                select(func.count(TelegramDialog.id)).where(
                    TelegramDialog.tenant_id == job.tenant_id,
                    TelegramDialog.selected.is_(True),
                )
            )
            review = await session.scalar(
                select(func.count(TelegramDialog.id)).where(
                    TelegramDialog.tenant_id == job.tenant_id,
                    TelegramDialog.requires_user_confirmation.is_(True),
                )
            )
        return {"selected": int(selected or 0), "needs_confirmation": int(review or 0)}

    async def message_preprocessing(self, job: JobLease) -> dict[str, object]:
        run_id = str(job.payload["analysis_run_id"])
        batches = await self.analysis.builder.build(
            run_id,
            history_window_days=int(job.payload.get("history_window_days", 30)),
        )
        return {"analysis_run_id": run_id, "batch_ids": batches}

    async def problem_deduplication(self, job: JobLease) -> dict[str, int]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count(OperationalProblem.id)).where(
                    OperationalProblem.tenant_id == job.tenant_id
                )
            )
        # Fingerprint has a database uniqueness constraint, so duplicates cannot commit.
        return {"problems": int(total or 0), "duplicates_removed": 0}

    async def report_delivery(self, job: JobLease) -> dict[str, str]:
        report_id = str(job.payload["report_id"])

        async def write(session: AsyncSession) -> str:
            report = await session.scalar(
                select(Report).where(
                    Report.id == report_id,
                    Report.tenant_id == job.tenant_id,
                    Report.status == "ready",
                )
            )
            if report is None:
                raise LookupError("ready report not found")
            # The inline bot and Mini App read this durable state; a transport adapter may
            # additionally push a Telegram notification without changing report readiness.
            report.delivery_status = "available"
            return report.id

        delivered_id = await self.transactions.run(write)
        return {"report_id": delivered_id, "delivery_status": "available"}

    async def statistics_refresh(self, job: JobLease) -> dict[str, int]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        async with self.session_factory() as session:
            reports = await session.scalar(
                select(func.count(Report.id)).where(Report.tenant_id == job.tenant_id)
            )
            problems = await session.scalar(
                select(func.count(OperationalProblem.id)).where(
                    OperationalProblem.tenant_id == job.tenant_id
                )
            )
        return {"reports": int(reports or 0), "problems": int(problems or 0)}

    async def session_health_check(self, job: JobLease) -> dict[str, str]:
        if job.tenant_id is None or self.connection_service is None:
            raise RuntimeError("Telegram session health is not configured")
        connection = await self.connection_service.check_health(job.tenant_id)
        return {"connection_id": connection.id, "health_status": connection.health_status}

    async def cleanup(self, _: JobLease) -> dict[str, int]:
        return {"expired_fsm_states": await self.storage.cleanup_expired()}

    async def retry_failed_job(self, job: JobLease) -> dict[str, object]:
        target_id = str(job.payload["job_id"])

        async def write(session: AsyncSession) -> bool:
            changed = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == target_id,
                    BackgroundJob.tenant_id == job.tenant_id,
                    BackgroundJob.status == "failed",
                )
                .values(
                    status="retry_scheduled",
                    scheduled_at=datetime.now(UTC),
                    finished_at=None,
                    last_error=None,
                )
            )
            return changed.rowcount == 1

        return {"job_id": target_id, "scheduled": await self.transactions.run(write)}
