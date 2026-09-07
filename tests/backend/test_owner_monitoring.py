from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from services.backend.bot import handlers
from services.backend.bot.keyboards import system_monitoring_menu
from services.backend.jobs.queue import JobDeferred, JobLease, SQLiteJobQueue
from services.backend.metrics import collect_runtime_metrics
from services.backend.models import (
    AIUsageCall,
    BackgroundJob,
    NotificationLog,
    RuntimeHealth,
)
from services.backend.observability import StructuredJSONFormatter
from services.backend.services import runtime_monitoring
from services.backend.services.owner_monitoring import (
    PlatformAlertMonitor,
    PlatformOwnerAlertDispatcher,
    build_operational_log_export,
    build_platform_summary_text,
)
from services.backend.services.runtime_monitoring import record_runtime_heartbeat


def test_structured_formatter_redacts_known_secret_shapes() -> None:
    token = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    record = logging.LogRecord(
        "ventrix",
        logging.ERROR,
        __file__,
        1,
        "request /bot%s/sendMessage authorization=Bearer-secret phone=+79990001122",
        (token,),
        None,
    )
    rendered = StructuredJSONFormatter().format(record)
    assert token not in rendered
    assert "Bearer-secret" not in rendered
    assert "+79990001122" not in rendered
    assert rendered.count("[REDACTED]") >= 3


def test_structured_formatter_redacts_safe_context_values_too() -> None:
    secret = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    record = logging.LogRecord("ventrix", logging.ERROR, __file__, 1, "failed", (), None)
    record.safe_context = {"error_code": f"token={secret}", "tenant_id": "tenant-1"}
    rendered = StructuredJSONFormatter().format(record)
    assert secret not in rendered
    assert json.loads(rendered)["tenant_id"] == "tenant-1"


def test_system_monitoring_keyboard_exposes_only_fixed_safe_actions() -> None:
    callbacks = [
        button.callback_data for row in system_monitoring_menu().inline_keyboard for button in row
    ]
    assert callbacks == [
        "owner:system",
        "owner:system:errors",
        "owner:system:logs:1",
        "owner:system:logs:2",
        "owner:activity",
        "owner:menu",
    ]


@pytest.mark.asyncio
async def test_system_log_export_is_denied_before_database_access(
    session_factory, settings
) -> None:
    query = SimpleNamespace(
        data="owner:system:logs:1",
        from_user=SimpleNamespace(id=settings.platform_owner_telegram_id + 1),
        answer=AsyncMock(),
        message=SimpleNamespace(answer_document=AsyncMock()),
    )
    await handlers.system_logs(query, session_factory, settings)
    query.answer.assert_awaited_once_with("Недоступно", show_alert=True)
    query.message.answer_document.assert_not_awaited()


def _alert_job(payload: dict[str, str]) -> JobLease:
    return JobLease(
        id="alert-job",
        tenant_id=None,
        telegram_account_id=None,
        dialog_id=None,
        correlation_id=None,
        job_type="platform.alert",
        category="notification",
        cost_class="light",
        payload=payload,
        attempts=0,
        max_attempts=3,
        locked_by="worker",
    )


@pytest.mark.asyncio
async def test_platform_alert_dispatcher_uses_allowlisted_message(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Response:
        status_code = 200
        content = b"{}"
        is_error = False

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True}

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object]):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr("services.backend.services.owner_monitoring.httpx.AsyncClient", Client)
    dispatcher = PlatformOwnerAlertDispatcher(
        api_base_url="https://telegram.invalid",
        bot_token="bot-secret",
        owner_telegram_id=42,
    )
    result = await dispatcher.dispatch(_alert_job({"code": "disk_low", "state": "active"}))
    assert result == {"status": "sent", "code": "disk_low", "state": "active"}
    assert calls[0][1]["chat_id"] == 42
    rendered = str(calls[0][1]["text"])
    assert "Заканчивается место на сервере" in rendered
    assert "Что это значит" in rendered
    assert "Что затронуто" in rendered
    assert "Что сделать" in rendered
    assert calls[0][1]["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "⚠️ Ошибки", "callback_data": "owner:system:errors"},
                {"text": "🟢 Состояние", "callback_data": "owner:system"},
            ]
        ]
    }
    assert "arbitrary" not in str(calls[0][1]["text"])


@pytest.mark.asyncio
async def test_platform_alert_dispatcher_respects_telegram_retry_after(monkeypatch) -> None:
    class Response:
        status_code = 429
        content = b"{}"
        is_error = True

        @staticmethod
        def json() -> dict[str, object]:
            return {"parameters": {"retry_after": 17}}

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object):
            return Response()

    monkeypatch.setattr("services.backend.services.owner_monitoring.httpx.AsyncClient", Client)
    dispatcher = PlatformOwnerAlertDispatcher(
        api_base_url="https://telegram.invalid",
        bot_token="bot-secret",
        owner_telegram_id=42,
    )
    with pytest.raises(JobDeferred) as raised:
        await dispatcher.dispatch(_alert_job({"code": "disk_low", "state": "active"}))
    assert raised.value.delay_seconds == 17


@pytest.mark.asyncio
async def test_runtime_heartbeat_upserts_one_bounded_component(session_factory) -> None:
    component = "worker:" + "very-long-instance-name-" * 5
    await record_runtime_heartbeat(
        session_factory,
        component,
        details={"pool": "realtime"},
    )
    await record_runtime_heartbeat(
        session_factory,
        component,
        details={"pool": "notification"},
    )
    async with session_factory() as session:
        rows = list(await session.scalars(select(RuntimeHealth)))
    assert len(rows) == 1
    assert len(rows[0].component) <= 64
    assert rows[0].details_json == {"pool": "notification"}


@pytest.mark.asyncio
async def test_runtime_heartbeat_loop_survives_transient_database_error(
    monkeypatch,
) -> None:
    calls = 0
    recovered = asyncio.Event()

    async def transient_heartbeat(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database contention")
        recovered.set()

    monkeypatch.setattr(runtime_monitoring, "record_runtime_heartbeat", transient_heartbeat)
    task = asyncio.create_task(
        runtime_monitoring.runtime_heartbeat_loop(
            object(),
            "worker:test",
            interval_seconds=0.001,
        )
    )
    await asyncio.wait_for(recovered.wait(), timeout=1)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_depth_is_exact_beyond_recent_metrics_window(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add_all(
            [
                BackgroundJob(
                    job_type="system.echo",
                    payload_json={},
                    status="pending",
                    scheduled_at=now,
                    category="general" if index < 2000 else "notification",
                    cost_class="light",
                )
                for index in range(2005)
            ]
        )
        await session.commit()
        metrics = await collect_runtime_metrics(session)
    assert metrics["queue"]["depth"] == 2005
    assert metrics["queue"]["depth_by_category"] == {
        "general": 2000,
        "notification": 5,
    }


@pytest.mark.asyncio
async def test_future_scheduled_job_is_not_reported_as_backlog(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            BackgroundJob(
                job_type="dialog.sla_check",
                payload_json={},
                status="scheduled",
                scheduled_at=now + timedelta(hours=2),
                category="reconciliation",
                cost_class="light",
            )
        )
        await session.commit()
        metrics = await collect_runtime_metrics(session)

    assert metrics["queue"]["depth"] == 0
    assert metrics["queue"]["oldest_job_age_seconds"] == 0


@pytest.mark.asyncio
async def test_lock_and_flood_alert_counters_only_cover_last_hour(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        old = BackgroundJob(
            job_type="system.echo",
            payload_json={},
            status="failed",
            scheduled_at=now - timedelta(hours=3),
            last_error="database is locked; flood wait",
        )
        recent = BackgroundJob(
            job_type="system.echo",
            payload_json={},
            status="failed",
            scheduled_at=now,
            last_error="database is locked; flood wait",
        )
        session.add_all([old, recent])
        await session.flush()
        old.updated_at = now - timedelta(hours=3)
        recent.updated_at = now
        await session.commit()
        metrics = await collect_runtime_metrics(session)
    assert metrics["sqlite"]["lock_failures_last_hour"] == 1
    assert metrics["telegram"]["flood_wait_failures"] == 1


@pytest.mark.asyncio
async def test_operational_export_is_bounded_and_omits_payloads(
    session_factory, make_service, tenant_payload
) -> None:
    secret = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
    now = datetime.now(UTC)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        session.add_all(
            [
                BackgroundJob(
                    tenant_id=tenant.id,
                    job_type="analysis.deep",
                    payload_json={"private_message": "Текст переписки", "token": secret},
                    status="failed",
                    scheduled_at=now,
                    category="ai_heavy",
                    cost_class="heavy",
                    attempts=1,
                    last_error=f"token={secret}",
                ),
                AIUsageCall(
                    tenant_id=tenant.id,
                    model="deepseek-test",
                    job_type="analysis.deep",
                    duration_ms=20,
                    status="failed",
                    error_code="deepseek_http_429",
                    occurred_at=now,
                ),
                NotificationLog(
                    tenant_id=tenant.id,
                    destination_type="manager",
                    destination_id=str(tenant.owner_telegram_user_id),
                    deduplication_key="safe-log-export",
                    status="failed",
                    criticality=90,
                    payload_json={"text": "Секретный текст клиента", "token": secret},
                    last_error_code="telegram_http_500",
                ),
            ]
        )
        await session.commit()
        content = await build_operational_log_export(session, hours=1, now=now)

    assert len(content) < 1_500_000
    rendered = content.decode()
    assert secret not in rendered
    assert "Текст переписки" not in rendered
    assert "Секретный текст клиента" not in rendered
    events = [json.loads(line) for line in rendered.splitlines()]
    assert {item["event"] for item in events} >= {
        "background_job_state",
        "ai_call_failed",
        "notification_state",
    }
    assert all({"meaning", "impact", "admin_action"} <= item.keys() for item in events)


@pytest.mark.asyncio
async def test_platform_summary_aggregates_all_active_projects_for_last_24_hours(
    session_factory, make_service, tenant_payload
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        first = await make_service(session).create_tenant(tenant_payload)
        second = await make_service(session).create_tenant(
            tenant_payload.model_copy(
                update={
                    "name": "Второй проект",
                    "owner_telegram_username": "second_owner",
                    "owner_telegram_user_id": tenant_payload.owner_telegram_user_id + 1,
                }
            )
        )
        session.add_all(
            [
                AIUsageCall(
                    tenant_id=first.id,
                    model="deepseek-test",
                    job_type="signal.ai_triage",
                    input_tokens=100,
                    output_tokens=20,
                    estimated_cost=0.01,
                    duration_ms=10,
                    status="success",
                    occurred_at=now,
                ),
                AIUsageCall(
                    tenant_id=second.id,
                    model="deepseek-test",
                    job_type="signal.ai_triage",
                    input_tokens=50,
                    output_tokens=10,
                    estimated_cost=0.02,
                    duration_ms=10,
                    status="failed",
                    error_code="deepseek_http_429",
                    occurred_at=now,
                ),
            ]
        )
        await session.commit()
        rendered = await build_platform_summary_text(session, now=now)

    assert "Активных проектов: <b>2</b>" in rendered
    assert "Токенов: <b>180</b>" in rendered
    assert "AI: <b>1</b>" in rendered
    assert "Второй проект" in rendered


@pytest.mark.asyncio
async def test_platform_alerts_are_transition_based_and_report_recovery(
    session_factory,
) -> None:
    queue = SQLiteJobQueue(session_factory)
    monitor = PlatformAlertMonitor(
        session_factory,
        queue,
        backlog_age_seconds=86_400,
        delivery_failure_count=100,
        sqlite_lock_count=100,
        disk_free_percent=101,
    )
    first = await monitor.evaluate()
    repeated = await monitor.evaluate()
    monitor.disk_free_percent = 0
    recovered = await monitor.evaluate()

    assert first == [{"code": "disk_low", "state": "active"}]
    assert repeated == []
    assert recovered == [{"code": "disk_low", "state": "recovered"}]
    async with session_factory() as session:
        jobs = list(
            await session.scalars(
                select(BackgroundJob)
                .where(BackgroundJob.job_type == "platform.alert")
                .order_by(BackgroundJob.created_at)
            )
        )
        assert (
            await session.scalar(
                select(func.count(RuntimeHealth.id)).where(
                    RuntimeHealth.component == "platform_monitor"
                )
            )
            == 1
        )
    assert [item.payload_json["code"] for item in jobs] == ["disk_low", "disk_low"]
    assert [item.payload_json["state"] for item in jobs] == ["active", "recovered"]
    assert all(isinstance(item.payload_json["free_percent"], float) for item in jobs)
