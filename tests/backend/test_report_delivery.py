from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.notifications import NotificationDispatcher
from services.backend.jobs.maintenance import MaintenanceJobHandlers
from services.backend.jobs.queue import JobLease
from services.backend.models import (
    AnalysisRun,
    Employee,
    GroupIntegration,
    NotificationLog,
    Report,
    ReportMetric,
    ReportSection,
    TenantMembership,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "disabled", "removed", "changed_chat"])
async def test_queued_group_card_rechecks_integration(
    session_factory, make_service, tenant_payload, make_group_bot, change
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        bot = await make_group_bot(session, tenant)
        group = GroupIntegration(
            tenant_id=tenant.id, telegram_chat_id=-100123, title="TEST", status="active",
            bot_instance_id=bot.id, approved_at=datetime.now(UTC),
            approved_by_telegram_user_id=tenant.owner_telegram_user_id,
        )
        session.add(group)
        await session.flush()
        log = NotificationLog(
            tenant_id=tenant.id,
            group_integration_id=group.id,
            destination_type="group",
            destination_id="-100123",
            deduplication_key="group-access-test",
            criticality=90,
            payload_json={"text": "Рабочая ситуация"},
        )
        session.add(log)
        if change == "disabled":
            group.notifications_enabled = False
        elif change == "removed":
            group.status = "inactive"
        elif change == "changed_chat":
            group.telegram_chat_id = -100999
        await session.commit()
    sender = SimpleNamespace(send=AsyncMock())
    result = await NotificationDispatcher(session_factory, sender).dispatch(
        SimpleNamespace(payload={"notification_id": log.id}, tenant_id=tenant.id)
    )
    assert result["status"] == ("sent" if change == "none" else "cancelled")
    assert sender.send.await_count == (1 if change == "none" else 0)


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def enqueue(self, job_type: str, payload: dict[str, object], **kwargs) -> str:
        self.calls.append({"job_type": job_type, "payload": payload, **kwargs})
        return f"job-{len(self.calls)}"


class RecordingSender:
    async def send(self, tenant_id, destination_id, text, reply_markup=None) -> None:
        return None


@pytest.mark.asyncio
async def test_report_delivery_uses_notification_pool_and_deduplicates_log(
    session_factory, make_service, tenant_payload
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        employee = Employee(
            tenant_id=tenant.id,
            display_name="Мария",
            telegram_user_id=555_000_777,
            telegram_username="maria",
        )
        session.add(employee)
        await session.flush()
        session.add(
            TenantMembership(
                tenant_id=tenant.id,
                telegram_user_id=employee.telegram_user_id,
                employee_id=employee.id,
                role="employee",
                status="active",
            )
        )
        extra_employees: list[Employee] = []
        for index in range(8):
            extra = Employee(
                tenant_id=tenant.id,
                display_name=f"Сотрудник {index + 2}",
                telegram_user_id=555_001_000 + index,
                telegram_username=f"employee_{index + 2}",
            )
            session.add(extra)
            await session.flush()
            session.add(
                TenantMembership(
                    tenant_id=tenant.id,
                    telegram_user_id=extra.telegram_user_id,
                    employee_id=extra.id,
                    role="employee",
                    status="active",
                )
            )
            extra_employees.append(extra)
        run = AnalysisRun(
            tenant_id=tenant.id,
            trigger="scheduled",
            status="completed",
            stage="completed",
            started_at=now - timedelta(hours=1),
            finished_at=now,
            correlation_id="report-delivery-test",
            metrics_json={},
        )
        session.add(run)
        await session.flush()
        report = Report(
            tenant_id=tenant.id,
            analysis_run_id=run.id,
            status="ready",
            period_start=now - timedelta(days=7),
            period_end=now,
            ready_at=now,
            summary="Рабочая сводка готова.",
        )
        session.add(report)
        await session.flush()
        session.add(
            ReportMetric(
                tenant_id=tenant.id,
                report_id=report.id,
                metric_key="messages",
                numeric_value=12,
                data_json={},
            )
        )
        session.add(
            ReportSection(
                tenant_id=tenant.id,
                report_id=report.id,
                section_key="employee_report",
                position=2,
                data_json={
                    "employees": [
                        {
                            "employee_id": employee.id,
                            "name": "Мария",
                            "messages_sent": 25,
                            "active_dialogs": 10,
                            "response_rate_denominator": 10,
                            "responded_dialogs": 4,
                            "response_rate_percent": 40,
                            "interests_confirmed": 2,
                            "clients_waiting": 2,
                            "open_promises": 1,
                            "missed_deadlines": 1,
                            "attention_items": [
                                {
                                    "problem_id": "problem-1",
                                    "dialog": "@client",
                                    "age_minutes": 370,
                                    "evidence": "Когда пришлёте договор?",
                                }
                            ],
                        },
                        *[
                            {
                                "employee_id": extra.id,
                                "name": extra.display_name,
                                "messages_sent": 1,
                                "active_dialogs": 1,
                                "clients_waiting": 0,
                                "open_promises": 0,
                                "missed_deadlines": 0,
                            }
                            for extra in extra_employees
                        ],
                    ]
                },
            )
        )
        session.add(
            ReportSection(
                tenant_id=tenant.id,
                report_id=report.id,
                section_key="ai_narrative",
                position=3,
                data_json={
                    "period_kind": "недельный",
                    "executive_summary": "Команда отвечала быстрее прошлого периода.",
                    "highlights": ["Мария подтвердила один созвон."],
                    "risks": ["Два клиента всё ещё ждут ответа."],
                },
            )
        )
        await session.commit()

    queue = RecordingQueue()
    handler = MaintenanceJobHandlers(
        session_factory,
        SimpleNamespace(queue=queue),
        connection_service=None,
        fsm_ttl_hours=24,
    )
    lease = JobLease(
        id="report-delivery",
        tenant_id=tenant.id,
        telegram_account_id=None,
        dialog_id=None,
        correlation_id=None,
        job_type="report_delivery",
        category="report",
        cost_class="light",
        payload={"report_id": report.id},
        attempts=0,
        max_attempts=3,
        locked_by="test",
    )
    first = await handler.report_delivery(lease)
    second = await handler.report_delivery(lease)

    assert first["notifications"] == second["notifications"] == 10
    assert {call["category"] for call in queue.calls} == {"notification"}
    assert {call["job_type"] for call in queue.calls} == {
        "notification.employee",
        "notification.manager",
    }
    async with session_factory() as session:
        assert await session.scalar(select(func.count(NotificationLog.id))) == 10
        notification = await session.scalar(
            select(NotificationLog).where(NotificationLog.destination_type == "manager")
        )
        employee_notification = await session.scalar(
            select(NotificationLog).where(
                NotificationLog.destination_type == "employee",
                NotificationLog.employee_id == employee.id,
            )
        )
        queued_notifications = list(
            await session.scalars(
                select(NotificationLog).where(
                    NotificationLog.destination_type.in_(("manager", "employee"))
                )
            )
        )
        assert sum(item.destination_type == "employee" for item in queued_notifications) == 9
        text = notification.payload_json["text"]
        assert "Недельная сводка" in text
        assert "Команда отвечала быстрее" in text
        assert "Мария подтвердила один созвон" in text
        assert "Мария" in text
        assert "@client" in text
        assert "6 ч" in text
        assert notification.payload_json["report_id"] == report.id
        personal_text = employee_notification.payload_json["text"]
        assert "Ответили: <b>4 из 10</b> (40%)" in personal_text
        assert "Подтверждённый интерес: <b>2</b>" in personal_text
        assert "Когда пришлёте договор?" in personal_text
        assert "только ваши показатели" in personal_text
        assert "Команда отвечала быстрее" not in personal_text

    dispatcher = NotificationDispatcher(session_factory, RecordingSender())
    for queued_notification in queued_notifications:
        await dispatcher.dispatch(JobLease(
            id="notification-delivery",
            tenant_id=tenant.id,
            telegram_account_id=None,
            dialog_id=None,
            correlation_id=report.id,
            job_type="notification.manager",
            category="notification",
            cost_class="light",
            payload={"notification_id": queued_notification.id},
            attempts=0,
            max_attempts=3,
            locked_by="test",
        ))
    async with session_factory() as session:
        stored_report = await session.get(Report, report.id)
        assert stored_report.delivery_status == "sent"
        assert stored_report.delivered_at is not None
        assert "Активные задачи: <b>3</b>" in text
        group = GroupIntegration(
            tenant_id=tenant.id,
            telegram_chat_id=-100999,
            title="Отключённая группа",
            status="active",
            notifications_enabled=False,
        )
        session.add(group)
        await session.flush()
        cancelled_delivery = NotificationLog(
            tenant_id=tenant.id,
            group_integration_id=group.id,
            destination_type="group",
            destination_id="-100999",
            criticality=0,
            deduplication_key=f"report:{report.id}:group:-100999",
            payload_json={"text": "Отчёт", "report_id": report.id},
        )
        session.add(cancelled_delivery)
        await session.commit()
    result = await dispatcher.dispatch(
        SimpleNamespace(
            payload={"notification_id": cancelled_delivery.id},
            tenant_id=tenant.id,
        )
    )
    assert result["status"] == "cancelled"
    async with session_factory() as session:
        assert (await session.get(Report, report.id)).delivery_status == "partial"
