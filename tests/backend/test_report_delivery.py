from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.notifications import NotificationDispatcher
from services.backend.jobs.maintenance import MaintenanceJobHandlers
from services.backend.jobs.queue import JobLease
from services.backend.models import (
    AnalysisRun,
    NotificationLog,
    Report,
    ReportMetric,
    ReportSection,
)


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
                            "name": "Мария",
                            "clients_waiting": 2,
                            "open_promises": 1,
                            "missed_deadlines": 1,
                        }
                    ]
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

    assert first["notifications"] == second["notifications"] == 1
    assert {call["category"] for call in queue.calls} == {"notification"}
    assert {call["job_type"] for call in queue.calls} == {"notification.manager"}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(NotificationLog.id))) == 1
        notification = await session.scalar(select(NotificationLog))
        text = notification.payload_json["text"]
        assert "Ежедневная сводка" in text
        assert "Мария" in text
        assert notification.payload_json["report_id"] == report.id

    dispatcher = NotificationDispatcher(session_factory, RecordingSender())
    await dispatcher.dispatch(
        JobLease(
            id="notification-delivery",
            tenant_id=tenant.id,
            telegram_account_id=None,
            dialog_id=None,
            correlation_id=report.id,
            job_type="notification.manager",
            category="notification",
            cost_class="light",
            payload={"notification_id": notification.id},
            attempts=0,
            max_attempts=3,
            locked_by="test",
        )
    )
    async with session_factory() as session:
        stored_report = await session.get(Report, report.id)
        assert stored_report.delivery_status == "sent"
        assert stored_report.delivered_at is not None
        assert "Активные задачи: <b>3</b>" in text
