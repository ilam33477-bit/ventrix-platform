from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..analysis.service import AnalysisPipelineService
from ..bot.sqlite_storage import SQLiteFSMStorage
from ..client_bots.links import private_bot_link
from ..config import get_settings
from ..database import SQLiteTransactionManager
from ..models import (
    BackgroundJob,
    BotInstance,
    Employee,
    GroupIntegration,
    NotificationLog,
    OperationalProblem,
    Report,
    ReportMetric,
    ReportSection,
    TelegramDialog,
    Tenant,
    TenantMembership,
)
from ..telegram_sessions.service import TelegramConnectionService
from .queue import JobLease


def _report_age(minutes: object) -> str:
    value = max(0, int(minutes or 0))
    if value >= 60 * 24:
        return f"{value // (60 * 24)} дн."
    if value >= 60:
        return f"{value // 60} ч"
    return f"{value} мин."


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

        async def write(session: AsyncSession) -> tuple[str, list[tuple[str, str]], str]:
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
            employee_rows = list(
                (employee_section.data_json if employee_section else {}).get("employees")
                or (employee_section.data_json if employee_section else {}).get("rows")
                or []
            )
            narrative_section = await session.scalar(
                select(ReportSection).where(
                    ReportSection.report_id == report.id,
                    ReportSection.section_key == "ai_narrative",
                )
            )
            narrative = narrative_section.data_json if narrative_section else {}
            period_kind = str(narrative.get("period_kind") or "ежедневный")
            report_title = {
                "ежедневный": "Ежедневная сводка",
                "недельный": "Недельная сводка",
                "месячный": "Месячная сводка",
            }.get(period_kind, "Рабочая сводка")
            executive_summary = escape(str(narrative.get("executive_summary") or report.summary))
            highlights = [escape(str(item)) for item in list(narrative.get("highlights") or [])[:4]]
            risks = [escape(str(item)) for item in list(narrative.get("risks") or [])[:4]]
            employee_blocks: list[str] = []
            employee_blocks_by_id: dict[str, str] = {}
            employee_attention_by_id: dict[str, str] = {}
            manager_attention: list[tuple[int, str]] = []
            for row_index, row in enumerate(employee_rows):
                open_tasks = int(row.get("open_promises", 0)) + int(row.get("clients_waiting", 0))
                response_minutes = row.get("average_response_minutes")
                response_change = row.get("response_time_change_percent")
                response_line = ""
                if response_minutes is not None:
                    response_line = (
                        f"\nСреднее время ответа: <b>{float(response_minutes):g} мин.</b>"
                    )
                    if response_change is not None:
                        direction = "медленнее" if float(response_change) > 0 else "быстрее"
                        response_line += (
                            f" ({abs(float(response_change)):g}% {direction} прошлого периода)"
                        )
                outcome_lines: list[str] = []
                contacted = int(row.get("response_rate_denominator", 0))
                responded = int(row.get("responded_dialogs", 0))
                if contacted:
                    outcome_lines.append(
                        f"Ответили: <b>{responded} из {contacted}</b> "
                        f"({float(row.get('response_rate_percent') or 0):g}%)"
                    )
                if int(row.get("interests_confirmed", 0)):
                    outcome_lines.append(
                        f"Подтверждённый интерес: <b>{int(row['interests_confirmed'])}</b>"
                    )
                if int(row.get("calls_scheduled", 0)):
                    outcome_lines.append(
                        f"Подтверждённых созвонов: <b>{int(row['calls_scheduled'])}</b>"
                    )
                if int(row.get("sales_confirmed", 0)):
                    sales_line = f"Подтверждённых продаж: <b>{int(row['sales_confirmed'])}</b>"
                    amounts = row.get("confirmed_sales_amounts") or {}
                    if amounts:
                        rendered = ", ".join(
                            f"{float(value):g} {escape(str(currency))}"
                            for currency, value in amounts.items()
                        )
                        sales_line += f" · {rendered}"
                    outcome_lines.append(sales_line)
                outcomes = list(row.get("business_outcomes") or [])
                if outcomes:
                    outcome_lines.append(
                        f"Факт периода: {escape(str(outcomes[0].get('summary') or ''))}"
                    )
                employee_block = (
                    "<blockquote>"
                    f"<b>{escape(str(row.get('name') or 'Сотрудник'))}</b>\n"
                    f"Активность: <b>{int(row.get('messages_sent', 0))}</b> сообщений "
                    f"в <b>{int(row.get('active_dialogs', 0))}</b> диалогах"
                    f"{response_line}\n"
                    f"Активные задачи: <b>{open_tasks}</b>\n"
                    f"Клиенты ждут ответа: <b>{int(row.get('clients_waiting', 0))}</b>\n"
                    f"Открытые обещания: <b>{int(row.get('open_promises', 0))}</b>\n"
                    f"Просрочено: <b>{int(row.get('missed_deadlines', 0))}</b>"
                    + ("\n" + "\n".join(outcome_lines) if outcome_lines else "")
                    + "</blockquote>"
                )
                attention_lines: list[str] = []
                for item in list(row.get("attention_items") or [])[:5]:
                    dialog = escape(str(item.get("dialog") or "Диалог"))
                    evidence = escape(str(item.get("evidence") or ""))[:180]
                    age_minutes = int(item.get("age_minutes") or 0)
                    line = f"• <b>{dialog}</b> — {_report_age(age_minutes)}"
                    if evidence:
                        line += f" · «{evidence}»"
                    attention_lines.append(line)
                    manager_attention.append((age_minutes, line))
                if row_index < 8:
                    employee_blocks.append(employee_block)
                if row.get("employee_id"):
                    employee_id = str(row["employee_id"])
                    employee_blocks_by_id[employee_id] = employee_block
                    if attention_lines:
                        employee_attention_by_id[employee_id] = (
                            "\n\n<b>Требуют внимания</b>\n"
                            + "\n".join(attention_lines)
                        )
            no_activity = int(metrics.get("messages", 0)) == 0
            partial_analysis = bool(metrics.get("analysis_partial", 0))
            text = (
                f"📊 <b>{report_title} · {escape(tenant.name)}</b>\n"
                f"{report.period_start:%d.%m.%Y} — {report.period_end:%d.%m.%Y}\n\n"
                f"{executive_summary}\n\n"
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
                + (
                    "<b>Главное за период</b>\n"
                    + "\n".join(f"• {item}" for item in highlights)
                    + "\n\n"
                    if highlights
                    else ""
                )
                + (
                    "<b>Что требует внимания</b>\n"
                    + "\n".join(f"• {item}" for item in risks)
                    + "\n\n"
                    if risks
                    else ""
                )
                + (
                    "<b>Текущие рабочие ситуации</b>\n"
                    + "\n".join(
                        item[1]
                        for item in sorted(manager_attention, reverse=True)[:5]
                    )
                    + "\n\n"
                    if manager_attention
                    else ""
                )
                + (
                    "<b>По сотрудникам</b>\n" + "\n".join(employee_blocks) + "\n\n"
                    if employee_blocks
                    else ""
                )
                + "Полная сводка и связанные ситуации доступны в Mini App."
            )[:4000]
            destinations: list[tuple[str, str, str | None, str | None, str]] = [
                ("manager", str(tenant.owner_telegram_user_id), None, None, text)
            ]
            groups = list(
                await session.scalars(
                    select(GroupIntegration).where(
                        GroupIntegration.tenant_id == tenant.id,
                        GroupIntegration.status == "active",
                        GroupIntegration.notifications_enabled.is_(True),
                        GroupIntegration.approved_at.is_not(None),
                    )
                )
            )
            destinations.extend(
                (
                    "group",
                    str(group.telegram_chat_id),
                    group.id,
                    None,
                    (
                        "📊 <b>Сводка проекта готова</b>\n\n"
                        "Откройте личный чат с ботом. "
                        "Он покажет только доступные вам данные."
                    ),
                )
                for group in groups
            )
            employee_ids = list(employee_blocks_by_id)
            report_employees = list(
                await session.scalars(
                    select(Employee)
                    .join(
                        TenantMembership,
                        TenantMembership.employee_id == Employee.id,
                    )
                    .where(
                        Employee.tenant_id == tenant.id,
                        Employee.id.in_(employee_ids),
                        Employee.status == "active",
                        Employee.notifications_enabled.is_(True),
                        Employee.telegram_user_id.is_not(None),
                        TenantMembership.tenant_id == tenant.id,
                        TenantMembership.telegram_user_id == Employee.telegram_user_id,
                        TenantMembership.status == "active",
                        TenantMembership.role.in_(("employee", "manager", "owner")),
                    )
                )
            ) if employee_ids else []
            for employee in report_employees:
                destinations.append(
                    (
                        "employee",
                        str(employee.telegram_user_id),
                        None,
                        employee.id,
                        (
                            f"📊 <b>{report_title} · {escape(employee.display_name)}</b>\n"
                            f"{report.period_start:%d.%m.%Y} — {report.period_end:%d.%m.%Y}\n\n"
                            f"{employee_blocks_by_id[employee.id]}"
                            f"{employee_attention_by_id.get(employee.id, '')}\n\n"
                            "В сообщении показаны только ваши показатели. "
                            "Подробности доступны в Mini App."
                        )[:4000],
                    )
                )
            queued: list[tuple[str, str]] = []
            delivery_states: list[str] = []
            mini_app_url = get_settings().client_mini_app_url
            report_url = None
            if mini_app_url:
                separator = "&" if "?" in mini_app_url else "?"
                report_url = f"{mini_app_url}{separator}section=reports&report_id={report.id}"
            for (
                destination_type,
                destination_id,
                group_id,
                employee_id,
                destination_text,
            ) in destinations:
                destination_report_url = report_url
                if destination_type == "group" and group_id:
                    destination_group = await session.get(GroupIntegration, group_id)
                    if destination_group and destination_group.bot_instance_id:
                        destination_bot = await session.get(
                            BotInstance, destination_group.bot_instance_id
                        )
                        if destination_bot is not None:
                            destination_report_url = private_bot_link(
                                destination_bot.username,
                                f"report_{report.id}",
                            )
                dedup = f"report:{report.id}:{destination_type}:{destination_id}"
                existing = await session.scalar(
                    select(NotificationLog).where(NotificationLog.deduplication_key == dedup)
                )
                if existing is None:
                    existing = NotificationLog(
                        tenant_id=tenant.id,
                        group_integration_id=group_id,
                        employee_id=employee_id,
                        destination_type=destination_type,
                        destination_id=destination_id,
                        deduplication_key=dedup,
                        criticality=0,
                        payload_json={
                            "text": destination_text,
                            "privacy_safe": True,
                            "report_id": report.id,
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Открыть в Ventrix AI",
                                            **(
                                                {"url": destination_report_url}
                                                if destination_report_url
                                                else {"callback_data": "client:reports"}
                                            ),
                                        }
                                    ]
                                ]
                            },
                        },
                    )
                    session.add(existing)
                    await session.flush()
                delivery_states.append(existing.status)
                if existing.status not in {"sent", "cancelled", "delivery_uncertain"}:
                    queued.append((existing.id, destination_type))
            report.delivery_status = (
                "pending" if queued
                else "sent" if all(state == "sent" for state in delivery_states)
                else "partial" if "sent" in delivery_states
                else "delivery_uncertain" if "delivery_uncertain" in delivery_states
                else "cancelled"
            )
            if not queued and "sent" in delivery_states:
                report.delivered_at = datetime.now(UTC)
            return report.id, queued, report.delivery_status

        delivered_id, notifications, delivery_status = await self.transactions.run(write)
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
            "delivery_status": "queued" if notifications else delivery_status,
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
