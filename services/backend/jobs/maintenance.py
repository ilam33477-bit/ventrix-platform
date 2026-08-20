from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..analysis.service import AnalysisPipelineService
from ..bot.sqlite_storage import SQLiteFSMStorage
from ..database import SQLiteTransactionManager
from ..models import (
    BackgroundJob,
    GroupIntegration,
    NotificationLog,
    OperationalProblem,
    Report,
    ReportMetric,
    ReportSection,
    TelegramDialog,
    Tenant,
    TenantSettings,
)
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

    async def report_delivery(self, job: JobLease) -> dict[str, object]:
        report_id = str(job.payload["report_id"])

        async def write(session: AsyncSession) -> tuple[str, list[tuple[str, str]]]:
            report = await session.scalar(
                select(Report).where(
                    Report.id == report_id,
                    Report.tenant_id == job.tenant_id,
                    Report.status == "ready",
                )
            )
            if report is None:
                raise LookupError("ready report not found")
            tenant = await session.get(Tenant, report.tenant_id)
            if tenant is None:
                raise LookupError("report tenant not found")
            metrics = dict(
                (
                    await session.execute(
                        select(ReportMetric.metric_key, ReportMetric.numeric_value).where(
                            ReportMetric.report_id == report.id
                        )
                    )
                ).all()
            )
            employee_section = await session.scalar(
                select(ReportSection).where(
                    ReportSection.report_id == report.id,
                    ReportSection.section_key == "employee_report",
                )
            )
            employee_rows = list((employee_section.data_json if employee_section else {}).get("employees") or [])
            employee_blocks: list[str] = []
            for row in employee_rows[:8]:
                open_tasks = int(row.get("open_promises", 0)) + int(row.get("clients_waiting", 0))
                employee_blocks.append(
                    "<blockquote>"
                    f"<b>{escape(str(row.get('name') or 'Сотрудник'))}</b>\n"
                    f"Активные задачи: <b>{open_tasks}</b>\n"
                    f"Клиенты ждут ответа: <b>{int(row.get('clients_waiting', 0))}</b>\n"
                    f"Открытые обещания: <b>{int(row.get('open_promises', 0))}</b>\n"
                    f"Просрочено: <b>{int(row.get('missed_deadlines', 0))}</b>"
                    "</blockquote>"
                )
            no_activity = int(metrics.get("messages", 0)) == 0
            partial_analysis = bool(metrics.get("analysis_partial", 0))
            text = (
                f"📊 <b>Ежедневная сводка · {escape(tenant.name)}</b>\n"
                f"{report.period_start:%d.%m.%Y} — {report.period_end:%d.%m.%Y}\n\n"
                "<blockquote>"
                f"Сообщений изучено: <b>{int(metrics.get('messages', 0))}</b>\n"
                f"Рабочих ситуаций: <b>{int(metrics.get('problems', 0))}</b>\n"
                f"Высокого приоритета: <b>{int(metrics.get('high', 0))}</b>\n"
                f"Среднего приоритета: <b>{int(metrics.get('medium', 0))}</b>"
                "</blockquote>\n\n"
                + (
                    "Новых рабочих сообщений за период нет. Ventrix продолжает мониторинг.\n\n"
                    if no_activity
                    else ""
                )
                + (
                    "Часть переписок будет перепроверена автоматически в следующем цикле.\n\n"
                    if partial_analysis
                    else ""
                )
                + ("<b>По сотрудникам</b>\n" + "\n".join(employee_blocks) + "\n\n" if employee_blocks else "")
                + "Полная сводка и связанные ситуации доступны в Mini App."
            )[:4000]
            destinations: list[tuple[str, str, str | None]] = [
                ("manager", str(tenant.owner_telegram_user_id), None)
            ]
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
            )
            groups = (
                list(
                    await session.scalars(
                        select(GroupIntegration).where(
                            GroupIntegration.tenant_id == tenant.id,
                            GroupIntegration.status == "active",
                            GroupIntegration.notifications_enabled.is_(True),
                        )
                    )
                )
                if settings and settings.group_reminders_enabled
                else []
            )
            destinations.extend(
                ("group", str(group.telegram_chat_id), group.id) for group in groups
            )
            queued: list[tuple[str, str]] = []
            for destination_type, destination_id, group_id in destinations:
                dedup = f"report:{report.id}:{destination_type}:{destination_id}"
                existing = await session.scalar(
                    select(NotificationLog).where(NotificationLog.deduplication_key == dedup)
                )
                if existing is None:
                    existing = NotificationLog(
                        tenant_id=tenant.id,
                        group_integration_id=group_id,
                        destination_type=destination_type,
                        destination_id=destination_id,
                        deduplication_key=dedup,
                        criticality=0,
                        payload_json={
                            "text": text,
                            "privacy_safe": True,
                            "report_id": report.id,
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Открыть отчёты",
                                            "callback_data": "client:reports",
                                        }
                                    ]
                                ]
                            },
                        },
                    )
                    session.add(existing)
                    await session.flush()
                if existing.status != "sent":
                    queued.append((existing.id, destination_type))
            report.delivery_status = "pending" if queued else "sent"
            if not queued:
                report.delivered_at = datetime.now(UTC)
            return report.id, queued

        delivered_id, notifications = await self.transactions.run(write)
        for notification_id, destination_type in notifications:
            await self.analysis.queue.enqueue(
                f"notification.{destination_type}",
                {"notification_id": notification_id},
                tenant_id=job.tenant_id,
                priority=35,
                idempotency_key=f"report-delivery:{notification_id}",
                correlation_id=report_id,
                category="notification",
            )
        return {
            "report_id": delivered_id,
            "delivery_status": "queued" if notifications else "sent",
            "notifications": len(notifications),
        }

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
