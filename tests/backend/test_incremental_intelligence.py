from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from services.backend.intelligence.ai_triage import AITriageService
from services.backend.intelligence.local_signals import LocalSignalEngine, parse_deadline
from services.backend.intelligence.message_relevance import dialogue_is_explicitly_closed
from services.backend.intelligence.notifications import NotificationOrchestrator
from services.backend.intelligence.reconciliation import ReconciliationService
from services.backend.intelligence.signals import SignalService
from services.backend.intelligence.triage import TriageResult, parse_triage_result
from services.backend.jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from services.backend.models import (
    AIUsageCall,
    BackgroundJob,
    Commitment,
    DialogState,
    Employee,
    GroupIntegration,
    NotificationLog,
    OperationalProblem,
    ProblemVerification,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramIncrementalCursor,
    TelegramMessage,
    TenantSettings,
)
from services.backend.services.encryption import EncryptionService
from services.backend.telegram_sessions.gateway import (
    LoginChallenge,
    LoginResult,
    MessageBatch,
    RemoteDialog,
    RemoteFolder,
    RemoteMessage,
)
from services.backend.telegram_sessions.incremental import IncrementalTelegramIngestion
from services.backend.telegram_sessions.service import TelegramConnectionService


def test_completed_sales_decline_is_not_an_open_customer_thread() -> None:
    completed_dialogue = [
        {"outgoing": True, "text": "Могу дать тестовый доступ на один день."},
        {"outgoing": False, "text": "Если честно, то не особо горю желанием."},
        {"outgoing": True, "text": "Понимаю, без проблем. Не буду настаивать."},
        {"outgoing": True, "text": "Если передумаете или появятся вопросы, я на связи."},
        {"outgoing": False, "text": "Хорошо, спасибо за предложение."},
    ]

    assert dialogue_is_explicitly_closed(completed_dialogue) is True


def test_new_customer_question_reopens_a_previously_closed_thread() -> None:
    dialogue_with_follow_up = [
        {"outgoing": False, "text": "Нет, спасибо, сейчас не интересно."},
        {"outgoing": True, "text": "Понял, без проблем. Если передумаете, я на связи."},
        {"outgoing": False, "text": "А сколько будет стоить доступ на месяц?"},
    ]

    assert dialogue_is_explicitly_closed(dialogue_with_follow_up) is False


class IncrementalGateway:
    def __init__(self) -> None:
        self.phone_by_session: dict[str, str] = {}
        self.messages: dict[int, list[RemoteMessage]] = {}
        self.fetch_after_ids: list[int] = []

    async def begin_login(self, phone: str) -> LoginChallenge:
        pending = f"pending:{phone}"
        self.phone_by_session[pending] = phone
        return LoginChallenge(pending, f"hash:{phone}")

    async def complete_login(
        self,
        session_string: str,
        phone: str,
        phone_code_hash: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> LoginResult:
        suffix = int(phone[-2:])
        return LoginResult(
            "connected", f"authorized:{phone}", 800_000 + suffix, f"employee_{suffix}", phone
        )

    async def list_folders(self, session_string: str) -> list[RemoteFolder]:
        return [RemoteFolder(10, "Работа")]

    async def list_dialogs(self, session_string: str) -> list[RemoteDialog]:
        return [RemoteDialog(1001, "Client Group", "client_group", "group", 10)]

    async def fetch_new_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        after_id: int,
        limit: int,
    ) -> MessageBatch:
        self.fetch_after_ids.append(after_id)
        available = [item for item in self.messages.get(dialog_id, []) if item.id > after_id]
        page = available[:limit]
        next_id = page[-1].id if page else after_id
        return MessageBatch(page, next_id, len(available) > len(page))

    async def terminate_session(self, session_string: str) -> None:
        return None


class FakeTriageProvider:
    def __init__(self, criticality: int = 92) -> None:
        self.calls = 0
        self.criticality = criticality

    async def generate_json(self, **kwargs):
        self.calls += 1
        return (
            json.dumps(
                {
                    "criticality": self.criticality,
                    "category": "contract_question",
                    "requires_immediate_attention": True,
                    "requires_employee_notification": True,
                    "requires_manager_notification": True,
                    "reason": "Клиент готов начать и запросил договор.",
                    "recommended_action": "Ответить клиенту и отправить договор.",
                    "recommended_deadline_minutes": 15,
                    "needs_deep_analysis": False,
                }
            ),
            {"input_tokens": 120, "output_tokens": 40},
        )


class RejectedTriageProvider(FakeTriageProvider):
    def __init__(self) -> None:
        super().__init__(criticality=20)


async def _tenant(session_factory, make_service, tenant_payload):
    async with session_factory() as session:
        return await make_service(session).create_tenant(tenant_payload)


async def _connection_with_dialog(
    session_factory, make_service, tenant_payload, encryption_key, gateway
):
    tenant = await _tenant(session_factory, make_service, tenant_payload)
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    connection = await service.begin_login(tenant.id, "+79990000011")
    connection = await service.complete_login(tenant.id, connection_id=connection.id, code="12345")
    await service.refresh_catalog(tenant.id, connection.id)
    await service.select_scope(
        tenant.id,
        10,
        personal_dialogs_consent=False,
        connection_id=connection.id,
    )
    async with session_factory() as session:
        dialog = await session.scalar(
            select(TelegramDialog).where(TelegramDialog.connection_id == connection.id)
        )
    return tenant, connection, dialog, service


@pytest.mark.asyncio
async def test_tenant_supports_multiple_independent_telegram_connections(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant = await _tenant(session_factory, make_service, tenant_payload)
    gateway = IncrementalGateway()
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    first = await service.begin_login(tenant.id, "+79990000011")
    first = await service.complete_login(tenant.id, connection_id=first.id, code="11111")
    second = await service.begin_login(tenant.id, "+79990000022")
    second = await service.complete_login(tenant.id, connection_id=second.id, code="22222")

    connections = await service.get_all(tenant.id)
    assert {item.id for item in connections} == {first.id, second.id}
    assert {item.telegram_user_id for item in connections} == {800_011, 800_022}


@pytest.mark.asyncio
async def test_dialog_sla_timer_is_durable_and_employee_reply_cancels_it(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    async with session_factory() as session:
        incoming = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=100,
            sender_id=77,
            sender_role="customer",
            sent_at=datetime.now(UTC),
            outgoing=False,
            body_text="Подскажите статус?",
            attachments_json=[],
        )
        session.add(incoming)
        await session.commit()
    queue = SQLiteJobQueue(session_factory)
    signals = SignalService(session_factory, queue)
    lease = JobLease(
        id="sla-incoming",
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
        dialog_id=dialog.id,
        correlation_id=None,
        job_type="signal.local_scan",
        category="realtime",
        cost_class="light",
        payload={"message_id": incoming.id},
        attempts=0,
        max_attempts=3,
        locked_by="sla-test",
    )
    await signals.local_scan_job(lease)

    async with session_factory() as session:
        state = await session.scalar(select(DialogState).where(DialogState.dialog_id == dialog.id))
        assert state.response_expected_message_id == incoming.id
        assert state.next_sla_check_at is not None
        outgoing = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=101,
            sender_id=connection.telegram_user_id,
            sender_role="account_owner",
            sent_at=datetime.now(UTC) + timedelta(seconds=1),
            outgoing=True,
            body_text="Да, отправлю сегодня.",
            attachments_json=[],
        )
        session.add(outgoing)
        await session.commit()
    second = JobLease(
        id="sla-outgoing",
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
        dialog_id=dialog.id,
        correlation_id=None,
        job_type="signal.local_scan",
        category="realtime",
        cost_class="light",
        payload={"message_id": outgoing.id},
        attempts=0,
        max_attempts=3,
        locked_by="sla-test",
    )
    await signals.local_scan_job(second)
    async with session_factory() as session:
        state = await session.scalar(select(DialogState).where(DialogState.dialog_id == dialog.id))
        assert state.response_expected_message_id is None
        assert state.next_sla_check_at is None


@pytest.mark.asyncio
async def test_ai_unanswered_candidate_cannot_create_problem_before_sla(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=501,
            sender_role="customer",
            sent_at=now,
            outgoing=False,
            body_text="Можно подробнее?",
            attachments_json=[],
        )
        session.add(message)
        await session.flush()
        signal = Signal(
            tenant_id=tenant.id,
            telegram_connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=message.id,
            fingerprint="unanswered-before-sla",
            signal_type="customer_question",
            local_score=90,
            criticality=90,
            status="candidate",
            reason="Клиент запросил подробности.",
            detected_at=now,
            metadata_json={},
        )
        session.add(signal)
        session.add(
            DialogState(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                awaiting_employee_since=now,
                response_expected_message_id=message.id,
                next_sla_check_at=now + timedelta(minutes=60),
                open_commitments_json=[],
                unresolved_questions_json=[],
            )
        )
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        await session.commit()
        signal_id = signal.id

    result = TriageResult(
        criticality=92,
        category="customer_question",
        requires_immediate_attention=True,
        requires_employee_notification=True,
        requires_manager_notification=True,
        reason="Client asked for more details.",
        recommended_action="Employee should respond.",
        recommended_deadline_minutes=15,
        needs_deep_analysis=False,
        message_class="business",
        business_relevance=True,
        conversation_state="WAITING_FOR_EMPLOYEE",
        response_required=True,
        action_required=True,
        issue_family="UNANSWERED_REQUEST",
        confidence=0.95,
    )
    problem_id = await AITriageService(
        session_factory, SQLiteJobQueue(session_factory), None, model="test"
    )._apply_result(signal_id, result, settings, False)
    assert problem_id is None
    async with session_factory() as session:
        stored_signal = await session.get(Signal, signal_id)
        state = await session.scalar(select(DialogState).where(DialogState.dialog_id == dialog.id))
        assert stored_signal.status == "triaged"
        assert stored_signal.reason == "Клиент запросил подробности."
        assert state.response_expected_message_id == message.id
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 0


@pytest.mark.asyncio
async def test_incremental_ingestion_is_idempotent_and_recovers_cursor_after_restart(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    now = datetime.now(UTC)
    gateway.messages[1001] = [
        RemoteMessage(1, 91, "client", now, None, False, "ок", []),
        RemoteMessage(
            2,
            91,
            "client",
            now + timedelta(seconds=1),
            None,
            False,
            "Готовы начинать, пришлите договор и реквизиты?",
            [],
        ),
    ]
    queue = SQLiteJobQueue(session_factory)
    ingestion = IncrementalTelegramIngestion(
        session_factory,
        EncryptionService(encryption_key),
        gateway,
        queue,
        batch_size=10,
    )
    job_id = await queue.enqueue(
        "telegram.fetch_updates",
        {},
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
    )
    lease = await queue.claim_next("incremental-test")
    assert lease is not None and lease.id == job_id
    first = await ingestion.fetch_updates(lease)
    await queue.complete(lease, first)

    restarted = IncrementalTelegramIngestion(
        session_factory,
        EncryptionService(encryption_key),
        gateway,
        queue,
        batch_size=10,
    )
    second = await restarted.fetch_updates(lease)

    async with session_factory() as session:
        message_count = await session.scalar(select(func.count(TelegramMessage.id)))
        signal_count = await session.scalar(select(func.count(Signal.id)))
        cursor = await session.scalar(
            select(TelegramIncrementalCursor).where(
                TelegramIncrementalCursor.dialog_id == dialog.id
            )
        )
        queued_triage = await session.scalar(
            select(func.count(BackgroundJob.id)).where(BackgroundJob.job_type == "signal.ai_triage")
        )
    assert first["messages"] == 2 and second["messages"] == 0
    # The acknowledgement "ок" is a terminal courtesy message, not a
    # separate business signal. Only the actionable request is queued.
    assert message_count == 2 and signal_count == 1
    assert cursor.last_message_id == 2
    assert gateway.fetch_after_ids == [0, 2]
    assert queued_triage == signal_count


def test_local_signal_engine_filters_low_value_and_scores_commercial_context() -> None:
    now = datetime.now(UTC)
    engine = LocalSignalEngine()
    low = TelegramMessage(
        telegram_message_id=1,
        sent_at=now,
        outgoing=False,
        body_text="спасибо",
        attachments_json=[],
    )
    important = TelegramMessage(
        telegram_message_id=2,
        sent_at=now,
        outgoing=False,
        body_text="Сколько стоит? Пришлите договор и реквизиты.",
        attachments_json=[],
    )
    assert engine.scan(low, []) == []
    candidates = engine.scan(important, [])
    assert {item.signal_type for item in candidates} >= {
        "commercial_question",
        "contract_question",
        "payment_question",
    }
    assert min(item.score for item in candidates) >= 65


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("отправлю сегодня к 18:00", "2026-08-04T18:00:00+03:00"),
        ("отправлю завтра", "2026-08-05T18:00:00+03:00"),
        ("отправлю послезавтра", "2026-08-06T18:00:00+03:00"),
        ("отправлю до пятницы", "2026-08-07T18:00:00+03:00"),
        ("отправлю в пятницу к 12:30", "2026-08-07T12:30:00+03:00"),
        ("отправлю 15.08", "2026-08-15T18:00:00+03:00"),
        ("отправлю 15 августа", "2026-08-15T18:00:00+03:00"),
        ("отправлю через два дня", "2026-08-06T18:00:00+03:00"),
        ("сделаю на следующей неделе", "2026-08-10T18:00:00+03:00"),
    ],
)
def test_deadline_parser_uses_tenant_timezone(phrase: str, expected: str) -> None:
    sent_at = datetime.fromisoformat("2026-08-04T10:00:00+03:00")
    deadline = parse_deadline(phrase, sent_at, "Europe/Moscow")
    assert deadline is not None and deadline.isoformat() == expected


def test_deadline_parser_rolls_time_and_year_forward() -> None:
    sent_at = datetime.fromisoformat("2026-12-20T19:00:00+03:00")
    assert parse_deadline("пришлю к 18:00", sent_at).isoformat() == ("2026-12-21T18:00:00+03:00")
    assert parse_deadline("пришлю 15.08", sent_at).isoformat() == ("2027-08-15T18:00:00+03:00")


def test_triage_json_is_strict_and_supports_one_controlled_repair() -> None:
    raw = """```json
    {"criticality":72,"category":"contract_question",
    "requires_immediate_attention":false,"requires_employee_notification":false,
    "requires_manager_notification":true,"reason":"Нужен договор",
    "recommended_action":"Ответить","recommended_deadline_minutes":30,
    "needs_deep_analysis":false,}
    ```"""
    result, repaired = parse_triage_result(raw)
    assert repaired is True and result.criticality == 72
    with pytest.raises(ValidationError):
        parse_triage_result("AI says this is probably important")


@pytest.mark.asyncio
async def test_critical_triage_records_usage_problem_and_privacy_safe_notifications(
    session_factory, make_service, tenant_payload, encryption_key, monkeypatch
) -> None:
    monkeypatch.setattr(
        "services.backend.intelligence.notifications.get_settings",
        lambda: SimpleNamespace(client_mini_app_url="https://mini.example"),
    )
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    queue = SQLiteJobQueue(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        managed_connection = await session.get(TelegramConnection, connection.id)
        managed_connection.username = "employee_account"
        employee = Employee(
            tenant_id=tenant.id,
            display_name="Менеджер",
            telegram_user_id=700001,
            criticality_threshold=85,
        )
        group = GroupIntegration(
            tenant_id=tenant.id,
            telegram_chat_id=dialog.telegram_dialog_id,
            title="Продажи",
            status="active",
            notifications_enabled=True,
            minimum_criticality=85,
        )
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=10,
            sender_id=99,
            sent_at=now,
            outgoing=False,
            body_text="Секретные условия клиента: пришлите договор",
            ingestion_source="live",
            attachments_json=[],
        )
        session.add_all([employee, group, message])
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        settings.group_reminders_enabled = True
        await session.flush()
        signal = Signal(
            tenant_id=tenant.id,
            telegram_connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=message.id,
            employee_id=employee.id,
            fingerprint="critical-triage",
            signal_type="contract_question",
            local_score=90,
            criticality=90,
            status="candidate",
            reason="local candidate",
            detected_at=now,
            metadata_json={"features": {"contract": True}},
        )
        session.add(signal)
        await session.commit()
        signal_id = signal.id
    job_id = await queue.enqueue(
        "signal.ai_triage",
        {"signal_id": signal_id},
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
        dialog_id=dialog.id,
        priority=JOB_PRIORITY["P0"],
    )
    lease = await queue.claim_next("triage-test")
    assert lease is not None and lease.id == job_id
    provider = FakeTriageProvider()
    result = await AITriageService(session_factory, queue, provider, model="deepseek-test").triage(
        lease
    )
    await queue.complete(lease, result)

    async with session_factory() as session:
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 1
        usage = await session.scalar(select(AIUsageCall))
        logs = list(await session.scalars(select(NotificationLog)))
    assert provider.calls == 1
    assert usage.input_tokens == 120 and usage.output_tokens == 40
    assert {item.destination_type for item in logs} == {"employee", "manager", "group"}
    group_payload = next(item.payload_json for item in logs if item.destination_type == "group")
    assert group_payload["privacy_safe"] is True
    assert "Секретные условия" not in group_payload["text"]
    manager_payload = next(item.payload_json for item in logs if item.destination_type == "manager")
    assert "Рабочий аккаунт:</b> @employee_account" in manager_payload["text"]
    assert "Контекст диалога" in manager_payload["text"]
    buttons = [
        button for row in manager_payload["reply_markup"]["inline_keyboard"] for button in row
    ]
    assert "Открыть чат" not in {button["text"] for button in buttons}
    system_button = next(button for button in buttons if button["text"] == "Посмотреть в системе")
    assert f"problem_id={result['problem_id']}" in system_button["web_app"]["url"]

    duplicate = await NotificationOrchestrator(session_factory, queue).plan_for_signal(
        signal_id, result["problem_id"]
    )
    assert duplicate == []


@pytest.mark.asyncio
async def test_configured_fast_lane_is_provisional_and_ai_can_cancel_it(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        settings.critical_fast_lane_rules = [
            {
                "id": "refund-claim",
                "enabled": True,
                "contains_all": ["возврат", "претенз"],
                "contains_any": [],
                "signal_types": ["complaint"],
                "criticality": 97,
            }
        ]
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=77,
            sender_id=90077,
            sent_at=now,
            outgoing=False,
            body_text="Требую возврат, направляю официальную претензию.",
            ingestion_source="live",
            attachments_json=[],
        )
        session.add(message)
        await session.commit()
        message_id = message.id

    queue = SQLiteJobQueue(session_factory)
    local_job_id = await queue.enqueue(
        "signal.local_scan",
        {"message_id": message_id},
        tenant_id=tenant.id,
        category="general",
    )
    local_lease = await queue.claim_next("local", allowed_categories=frozenset({"general"}))
    assert local_lease is not None and local_lease.id == local_job_id
    await SignalService(session_factory, queue).local_scan_job(local_lease)
    await queue.complete(local_lease)

    async with session_factory() as session:
        signal = await session.scalar(select(Signal).where(Signal.signal_type == "complaint"))
        provisional = await session.scalar(
            select(NotificationLog).where(NotificationLog.signal_id == signal.id)
        )
    assert signal.criticality == 97
    assert provisional.payload_json["provisional"] is True

    triage_lease = await queue.claim_next("ai", allowed_categories=frozenset({"ai_fast"}))
    assert triage_lease is not None
    await AITriageService(
        session_factory,
        queue,
        RejectedTriageProvider(),
        model="deepseek-test",
    ).triage(triage_lease)
    async with session_factory() as session:
        provisional = await session.get(NotificationLog, provisional.id)
    assert provisional.status == "cancelled"
    assert provisional.payload_json["cancelled"] is True


@pytest.mark.asyncio
async def test_notification_cooldown_is_problem_scoped_and_critical_bypasses_it(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        settings.notification_immediate_threshold = 95
        signals: list[Signal] = []
        for index, (signal_type, criticality) in enumerate(
            (("complaint", 80), ("contract_question", 80), ("complaint", 82), ("complaint", 99)),
            start=1,
        ):
            message = TelegramMessage(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                telegram_message_id=100 + index,
                sent_at=now + timedelta(seconds=index),
                outgoing=False,
                body_text=f"Событие {index}",
                ingestion_source="live",
                attachments_json=[],
            )
            session.add(message)
            await session.flush()
            signal = Signal(
                tenant_id=tenant.id,
                telegram_connection_id=connection.id,
                dialog_id=dialog.id,
                source_message_id=message.id,
                fingerprint=f"cooldown-{index}",
                signal_type=signal_type,
                local_score=criticality,
                criticality=criticality,
                reason="Проверка cooldown",
                detected_at=message.sent_at,
                metadata_json={},
            )
            session.add(signal)
            signals.append(signal)
        await session.commit()

    orchestrator = NotificationOrchestrator(session_factory, SQLiteJobQueue(session_factory))
    first = await orchestrator.plan_for_signal(signals[0].id)
    different_problem = await orchestrator.plan_for_signal(signals[1].id)
    equivalent = await orchestrator.plan_for_signal(signals[2].id)
    critical = await orchestrator.plan_for_signal(signals[3].id)

    assert len(first) == 1
    assert len(different_problem) == 1
    assert equivalent == []
    assert len(critical) == 1


@pytest.mark.asyncio
async def test_cross_connection_signal_provenance_never_downgrades_lifecycle_status(
    session_factory, make_service, tenant_payload
) -> None:
    tenant = await _tenant(session_factory, make_service, tenant_payload)
    async with session_factory() as session:
        connections = [
            TelegramConnection(
                tenant_id=tenant.id,
                telegram_user_id=810_000 + index,
                status="ready",
            )
            for index in range(2)
        ]
        session.add_all(connections)
        await session.flush()
        dialogs: list[TelegramDialog] = []
        messages: list[TelegramMessage] = []
        for index, connection in enumerate(connections):
            dialog = TelegramDialog(
                tenant_id=tenant.id,
                connection_id=connection.id,
                telegram_dialog_id=-100123456,
                canonical_peer_id="-100123456",
                title="Shared sales group",
                dialog_type="group",
                source="folder",
                selected=True,
            )
            session.add(dialog)
            await session.flush()
            dialogs.append(dialog)
            message = TelegramMessage(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                telegram_message_id=77,
                sender_id=990_001,
                sent_at=datetime.now(UTC) + timedelta(milliseconds=index),
                outgoing=False,
                body_text="Клиент просит прислать договор.",
                attachments_json=[],
            )
            session.add(message)
            messages.append(message)
        await session.flush()
        service = SignalService(session_factory, SQLiteJobQueue(session_factory))
        first = await service.scan_message(session, messages[0], [])
        contract_signal = next(item for item in first if item.signal_type == "contract_question")
        contract_signal.status = "problem_created"
        await service.scan_message(session, messages[1], [])
        await session.commit()

    async with session_factory() as session:
        signals = list(
            await session.scalars(select(Signal).where(Signal.signal_type == "contract_question"))
        )
    assert len(signals) == 1
    assert signals[0].status == "problem_created"
    provenance = signals[0].metadata_json["source_provenance"]
    assert {item["connection_id"] for item in provenance} == {
        connections[0].id,
        connections[1].id,
    }


@pytest.mark.asyncio
async def test_ai_triage_idempotency_changes_only_for_meaningful_message_edit(
    session_factory, make_service, tenant_payload
) -> None:
    tenant = await _tenant(session_factory, make_service, tenant_payload)
    async with session_factory() as session:
        connection = TelegramConnection(
            tenant_id=tenant.id,
            telegram_user_id=820_001,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=8201,
            title="Edit test",
            dialog_type="personal",
            source="personal",
            selected=True,
        )
        session.add(dialog)
        await session.flush()
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sent_at=datetime.now(UTC),
            outgoing=False,
            body_text="Пришлите договор",
            attachments_json=[],
        )
        session.add(message)
        await session.flush()
        signal = Signal(
            tenant_id=tenant.id,
            telegram_connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=message.id,
            fingerprint="edit-triage-signal",
            signal_type="contract_question",
            local_score=90,
            criticality=90,
            status="candidate",
            reason="Нужен договор",
            detected_at=message.sent_at,
            metadata_json={},
        )
        session.add(signal)
        await session.commit()

    queue = SQLiteJobQueue(session_factory)
    service = SignalService(session_factory, queue)
    original = await service.enqueue_triage([signal])
    duplicate_original = await service.enqueue_triage([signal])
    async with session_factory() as session:
        stored_message = await session.get(TelegramMessage, message.id)
        stored_message.body_text = "Срочно пришлите подписанный договор сегодня"
        stored_message.edited_at = datetime.now(UTC) + timedelta(seconds=1)
        stored_signal = await session.get(Signal, signal.id)
        stored_signal.status = "superseded"
        await session.commit()
    edited = await service.enqueue_triage([stored_signal])
    duplicate_edit = await service.enqueue_triage([stored_signal])

    async with session_factory() as session:
        jobs = list(
            await session.scalars(
                select(BackgroundJob).where(BackgroundJob.job_type == "signal.ai_triage")
            )
        )
    assert original == duplicate_original
    assert edited == duplicate_edit
    assert original != edited
    assert len(jobs) == 2
    assert len({job.payload_json["source_version"] for job in jobs}) == 2


@pytest.mark.asyncio
async def test_commitment_deadline_check_is_targeted_and_idempotent(
    session_factory, make_service, tenant_payload
) -> None:
    tenant = await _tenant(session_factory, make_service, tenant_payload)
    now = datetime.now(UTC)
    async with session_factory() as session:
        connection = TelegramConnection(
            tenant_id=tenant.id,
            telegram_user_id=830_001,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=8301,
            title="Deadline test",
            dialog_type="personal",
            source="personal",
            selected=True,
        )
        session.add(dialog)
        await session.flush()
        commitments: list[Commitment] = []
        for index in range(2):
            source = TelegramMessage(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                telegram_message_id=index + 1,
                sent_at=now - timedelta(hours=3 - index),
                outgoing=True,
                body_text=f"Обещание {index}",
                attachments_json=[],
            )
            session.add(source)
            await session.flush()
            commitment = Commitment(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                source_message_id=source.id,
                fingerprint=f"targeted-deadline-{index}",
                commitment_type="employee_promise",
                expected_action=f"Выполнить обещание {index}",
                deadline_at=now - timedelta(hours=1),
                status="open",
                confidence=0.9,
                metadata_json={},
            )
            session.add(commitment)
            commitments.append(commitment)
        await session.commit()

    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue(
        "commitment.deadline_check",
        {"commitment_id": commitments[0].id},
        tenant_id=tenant.id,
        category="reconciliation",
    )
    lease = await queue.claim_next(
        "deadline-target", allowed_categories=frozenset({"reconciliation"})
    )
    assert lease is not None and lease.id == job_id
    reconciliation = ReconciliationService(session_factory, queue)
    first = await reconciliation.deadline_check(lease)
    second = await reconciliation.deadline_check(lease)

    async with session_factory() as session:
        problems = list(await session.scalars(select(OperationalProblem)))
        target = await session.get(Commitment, commitments[0].id)
        unrelated = await session.get(Commitment, commitments[1].id)
    assert first["overdue_created"] == 1
    assert second["overdue_created"] == 0
    assert len(problems) == 1 and problems[0].commitment_id == target.id
    assert target.last_checked_at is not None
    assert unrelated.last_checked_at is None


@pytest.mark.asyncio
async def test_hourly_reconciliation_creates_overdue_problem_once_and_then_resolves(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    gateway = IncrementalGateway()
    tenant, connection, dialog, _ = await _connection_with_dialog(
        session_factory, make_service, tenant_payload, encryption_key, gateway
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        employee = Employee(tenant_id=tenant.id, display_name="Менеджер")
        session.add(employee)
        await session.flush()
        stored_connection = await session.get(type(connection), connection.id)
        stored_connection.assigned_employee_id = employee.id
        source = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=20,
            sender_id=connection.telegram_user_id,
            sent_at=now - timedelta(hours=2),
            outgoing=True,
            body_text="Отправлю договор в течение часа",
            attachments_json=[],
        )
        session.add(source)
        await session.flush()
        commitment = Commitment(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=source.id,
            responsible_employee_id=employee.id,
            fingerprint="overdue-commitment",
            commitment_type="employee_promise",
            expected_action="Отправить договор",
            deadline_at=now - timedelta(hours=1),
            status="open",
            confidence=0.9,
            metadata_json={},
        )
        session.add(commitment)
        await session.commit()
    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("analysis.hourly", {}, tenant_id=tenant.id)
    lease = await queue.claim_next("reconcile-test")
    assert lease is not None and lease.id == job_id
    reconciliation = ReconciliationService(session_factory, queue)
    first = await reconciliation.reconcile(lease)
    second = await reconciliation.reconcile(lease)
    assert first["overdue_created"] == 1 and second["overdue_created"] == 0
    unchanged = await reconciliation.reconcile(lease)
    async with session_factory() as session:
        verification_count = int(
            await session.scalar(select(func.count(ProblemVerification.id))) or 0
        )
    assert unchanged == {
        "overdue_created": 0,
        "commitments_completed": 0,
        "problems_resolved": 0,
    }
    assert verification_count == 1

    async with session_factory() as session:
        completion = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=21,
            sender_id=connection.telegram_user_id,
            sent_at=datetime.now(UTC) + timedelta(seconds=1),
            outgoing=True,
            body_text="Готово, отправил договор",
            attachments_json=[],
        )
        session.add(completion)
        await session.commit()
    completed = await reconciliation.reconcile(lease)
    async with session_factory() as session:
        stored_commitment = await session.scalar(select(Commitment))
        problem = await session.scalar(select(OperationalProblem))
    assert completed["commitments_completed"] == 1
    assert stored_commitment.status == "completed" and problem.status == "auto_resolved"


@pytest.mark.asyncio
async def test_queue_claims_critical_triage_before_scheduled_report(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    report = await queue.enqueue("report.company", {}, priority=JOB_PRIORITY["P4"])
    critical = await queue.enqueue("signal.ai_triage", {}, priority=JOB_PRIORITY["P0"])
    lease = await queue.claim_next("priority-test")
    assert lease is not None and lease.id == critical and lease.id != report
