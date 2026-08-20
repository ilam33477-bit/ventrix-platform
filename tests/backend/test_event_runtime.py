from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.local_signals import LocalSignalEngine
from services.backend.intelligence.message_relevance import classify_message_relevance
from services.backend.intelligence.reconciliation import ReconciliationService
from services.backend.intelligence.signals import SignalService
from services.backend.jobs.queue import JobLease, SQLiteJobQueue
from services.backend.models import (
    BackgroundJob,
    DialogState,
    MonitoredSource,
    OperationalProblem,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantSettings,
)
from services.backend.services.client_drafts import OwnerClientDraftService
from services.backend.services.encryption import EncryptionService
from services.backend.services.system_secrets import (
    SystemSecretService,
    load_runtime_secret_overrides,
    mask_secret,
)
from services.backend.telegram_sessions.event_ingestion import TelegramEventIngestion
from services.backend.telegram_sessions.runtime import is_automated_private_entity


def event_lease(
    tenant_id: str,
    connection_id: str,
    peer_id: str,
    source_type: str,
    *,
    is_automated: bool = False,
) -> JobLease:
    return JobLease(
        id=f"event-{peer_id}",
        tenant_id=tenant_id,
        telegram_account_id=connection_id,
        dialog_id=None,
        correlation_id="correlation-1",
        job_type="telegram.ingest_event",
        category="realtime",
        cost_class="light",
        payload={
            "event_type": "new",
            "canonical_peer_id": peer_id,
            "telegram_dialog_id": int(peer_id),
            "dialog_title": "Рабочий чат",
            "source_type": source_type,
            "is_automated": is_automated,
            "telegram_message_id": 10,
            "sender_id": 700,
            "sent_at": datetime.now(UTC).isoformat(),
            "outgoing": False,
            "text": "Когда будет договор?",
            "attachments": [],
            "ingestion_source": "live",
        },
        attempts=0,
        max_attempts=5,
        locked_by="runtime-test",
    )


@pytest.mark.asyncio
async def test_personal_live_event_is_default_and_group_requires_opt_in(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready", telegram_user_id=900)
        session.add(connection)
        await session.commit()
    queue = SQLiteJobQueue(session_factory)
    ingestion = TelegramEventIngestion(session_factory, queue)

    personal = await ingestion.ingest(event_lease(tenant.id, connection.id, "101", "personal"))
    ignored = await ingestion.ingest(event_lease(tenant.id, connection.id, "202", "group"))
    assert personal["message_id"]
    assert ignored == {"ignored": True, "reason": "source_not_monitored"}

    async with session_factory() as session:
        session.add(
            MonitoredSource(
                tenant_id=tenant.id,
                connection_id=connection.id,
                canonical_peer_id="202",
                source_type="group",
                added_via="group_link",
                title="Рабочая группа",
            )
        )
        await session.commit()
    accepted = await ingestion.ingest(event_lease(tenant.id, connection.id, "202", "group"))
    assert accepted["message_id"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(TelegramMessage.id))) == 2
        scans = list(
            await session.scalars(
                select(BackgroundJob).where(BackgroundJob.job_type == "signal.scan_batch")
            )
        )
        assert len(scans) == 2


@pytest.mark.asyncio
async def test_private_bot_event_is_excluded_before_message_analysis(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.commit()
    ingestion = TelegramEventIngestion(session_factory, SQLiteJobQueue(session_factory))

    result = await ingestion.ingest(
        event_lease(
            tenant.id,
            connection.id,
            "178220800",
            "personal",
            is_automated=True,
        )
    )

    assert result == {"ignored": True, "reason": "automated_account"}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(TelegramMessage.id))) == 0
        assert await session.scalar(select(func.count(BackgroundJob.id))) == 0


def test_telegram_private_entity_classifier_rejects_non_humans() -> None:
    assert is_automated_private_entity(SimpleNamespace(bot=True, username="SpamBot"))
    assert is_automated_private_entity(SimpleNamespace(support=True, username=None))
    assert is_automated_private_entity(SimpleNamespace(deleted=True, username=None))
    assert is_automated_private_entity(SimpleNamespace(is_self=True, username="me"))
    assert not is_automated_private_entity(SimpleNamespace(username="real_customer"))


@pytest.mark.parametrize(
    ("text", "expected_class"),
    [
        ("Код для входа в Telegram: 56818. Не давайте код никому.", "service"),
        ("Вадим, добро пожаловать в группу Crypto Taverna Chat.", "service"),
        ("Казино дарит бесплатный бонус — успейте забрать!", "advertising"),
        ("Хорошо, спасибо", "social"),
        ("Договорились 🤝", "social"),
        ("Да", "social"),
        ("Понял, спасибо", "social"),
        ("Нет, спасибо за предложение", "social"),
        ("Спасибо, не интересует", "social"),
        ("👌", "social"),
        ("Подпишитесь на канал, чтобы получать новые вакансии", "advertising"),
        ("Клиент просит прислать договор и счёт до пятницы.", "business"),
    ],
)
def test_message_relevance_separates_service_ads_and_business(
    text: str, expected_class: str
) -> None:
    result = classify_message_relevance(text)
    assert result.message_class == expected_class
    assert result.business_relevant is (expected_class == "business")


@pytest.mark.asyncio
async def test_sla_check_discards_stale_timer_for_automated_dialog(
    session_factory, make_service, tenant_payload
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=777000,
            canonical_peer_id="777000",
            title="Telegram",
            dialog_type="personal",
            source="history",
            classification="automated_account",
            selected=False,
            excluded=True,
        )
        session.add(dialog)
        await session.flush()
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sender_role="customer",
            sent_at=now - timedelta(hours=2),
            outgoing=False,
            body_text="Код для входа в Telegram: 56818.",
            attachments_json=[],
        )
        session.add(message)
        await session.flush()
        session.add(
            DialogState(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                awaiting_employee_since=message.sent_at,
                response_expected_message_id=message.id,
                next_sla_check_at=now - timedelta(hours=1),
                open_commitments_json=[],
                unresolved_questions_json=[],
            )
        )
        await session.commit()

    queue = SQLiteJobQueue(session_factory)
    result = await ReconciliationService(session_factory, queue).sla_check(
        JobLease(
            id="automated-sla",
            tenant_id=tenant.id,
            telegram_account_id=connection.id,
            dialog_id=dialog.id,
            correlation_id=None,
            job_type="dialog.sla_check",
            category="reconciliation",
            cost_class="light",
            payload={"dialog_id": dialog.id, "expected_message_id": message.id},
            attempts=0,
            max_attempts=3,
            locked_by="test",
        )
    )
    assert result == {"created": False, "problem_id": None}
    async with session_factory() as session:
        state = await session.scalar(select(DialogState))
        assert state.response_expected_message_id is None
        assert state.next_sla_check_at is None
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 0


async def _sla_fixture(session_factory, make_service, tenant_payload, *, minutes_ago: int):
    now = datetime.now(UTC)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=880001,
            canonical_peer_id="880001",
            title="Клиент",
            dialog_type="personal",
            source="live",
            classification="human_dialog",
            selected=True,
            excluded=False,
        )
        session.add(dialog)
        await session.flush()
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sender_role="customer",
            sent_at=now - timedelta(minutes=minutes_ago),
            outgoing=False,
            body_text="Можно подробнее?",
            attachments_json=[],
        )
        session.add(message)
        await session.flush()
        session.add(
            DialogState(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                awaiting_employee_since=message.sent_at,
                response_expected_message_id=message.id,
                next_sla_check_at=message.sent_at + timedelta(minutes=60),
                open_commitments_json=[],
                unresolved_questions_json=[],
            )
        )
        await session.commit()
    return tenant, connection, dialog, message


def _sla_lease(tenant, connection, dialog, message) -> JobLease:
    return JobLease(
        id="sla-regression",
        tenant_id=tenant.id,
        telegram_account_id=connection.id,
        dialog_id=dialog.id,
        correlation_id=None,
        job_type="dialog.sla_check",
        category="reconciliation",
        cost_class="light",
        payload={"dialog_id": dialog.id, "expected_message_id": message.id},
        attempts=0,
        max_attempts=3,
        locked_by="test",
    )


@pytest.mark.asyncio
async def test_sla_never_creates_problem_before_persisted_deadline(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, connection, dialog, message = await _sla_fixture(
        session_factory, make_service, tenant_payload, minutes_ago=10
    )
    result = await ReconciliationService(session_factory, SQLiteJobQueue(session_factory)).sla_check(
        _sla_lease(tenant, connection, dialog, message)
    )
    assert result == {"created": False, "problem_id": None}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 0


@pytest.mark.asyncio
async def test_sla_uses_frozen_deadline_and_duplicate_job_is_idempotent(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, connection, dialog, message = await _sla_fixture(
        session_factory, make_service, tenant_payload, minutes_ago=61
    )
    async with session_factory() as session:
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant.id)
        )
        settings.response_sla_minutes = 180
        await session.commit()
    service = ReconciliationService(session_factory, SQLiteJobQueue(session_factory))
    first = await service.sla_check(_sla_lease(tenant, connection, dialog, message))
    second = await service.sla_check(_sla_lease(tenant, connection, dialog, message))
    assert first["problem_id"] == second["problem_id"]
    async with session_factory() as session:
        problem = await session.scalar(select(OperationalProblem))
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 1
        assert "60 мин." in problem.explanation
        assert "180 мин." not in problem.explanation


@pytest.mark.asyncio
async def test_employee_reply_seconds_before_deadline_makes_sla_job_a_noop(
    session_factory, make_service, tenant_payload
) -> None:
    tenant, connection, dialog, message = await _sla_fixture(
        session_factory, make_service, tenant_payload, minutes_ago=61
    )
    async with session_factory() as session:
        session.add(
            TelegramMessage(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                telegram_message_id=2,
                sender_role="account_owner",
                sent_at=message.sent_at + timedelta(minutes=59, seconds=59),
                outgoing=True,
                body_text="Конечно, сейчас расскажу подробнее.",
                attachments_json=[],
            )
        )
        await session.commit()
    result = await ReconciliationService(session_factory, SQLiteJobQueue(session_factory)).sla_check(
        _sla_lease(tenant, connection, dialog, message)
    )
    assert result == {"created": False, "problem_id": None}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(OperationalProblem.id))) == 0


@pytest.mark.asyncio
async def test_historical_bot_message_is_not_scanned_even_if_previously_selected(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=178220800,
            canonical_peer_id="178220800",
            title="Spam Info Bot",
            dialog_type="personal",
            source="history",
            classification="automated_account",
            selected=True,
            excluded=False,
        )
        session.add(dialog)
        await session.flush()
        message = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sender_role="customer",
            sent_at=datetime.now(UTC),
            outgoing=False,
            body_text="Ваш аккаунт свободен от ограничений.",
        )
        session.add(message)
        await session.commit()
    queue = SQLiteJobQueue(session_factory)
    result = await SignalService(session_factory, queue).scan_batch_job(
        JobLease(
            id="bot-history-scan",
            tenant_id=tenant.id,
            telegram_account_id=connection.id,
            dialog_id=dialog.id,
            correlation_id=None,
            job_type="signal.scan_batch",
            category="historical",
            cost_class="light",
            payload={"message_ids": [message.id]},
            attempts=0,
            max_attempts=3,
            locked_by="test",
        )
    )
    assert result == {"signals": 0, "triage_jobs": 0}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Signal.id))) == 0


def test_invoice_fast_lane_needs_invoice_metadata() -> None:
    engine = LocalSignalEngine()
    common = {
        "telegram_message_id": 1,
        "sent_at": datetime.now(UTC),
        "outgoing": False,
        "body_text": "Файл во вложении",
        "sender_role": "customer",
    }
    random_pdf = TelegramMessage(
        **common,
        attachments_json=[{"name": "presentation.pdf", "mime_type": "application/pdf"}],
    )
    invoice = TelegramMessage(
        **common,
        attachments_json=[{"name": "invoice_123.pdf", "mime_type": "application/pdf"}],
    )
    assert "invoice_received" not in {item.signal_type for item in engine.scan(random_pdf, [])}
    assert "invoice_received" in {item.signal_type for item in engine.scan(invoice, [])}


class FakeDraftProvider:
    async def generate_json(self, **kwargs):
        current = kwargs["payload"].get("current_draft")
        payload = current or {
            "name": "Northwind",
            "owner_name": "Иван",
            "owner_telegram_user_id": 123456,
            "owner_telegram_username": "northwind_owner",
            "niche": "B2B продажи",
            "business_description": "Поставляет оборудование компаниям",
            "products_services": "Оборудование и сервис",
            "target_audience": "Производственные компании",
            "timezone": "Europe/Moscow",
            "response_sla_minutes": 60,
            "critical_problem_criteria": "Жалоба, возврат или потерянный счёт",
            "daily_report_time": "09:00",
        }
        if current:
            payload = {**payload, "response_sla_minutes": 30}
        return json.dumps(payload, ensure_ascii=False), {"input_tokens": 10, "output_tokens": 20}


@pytest.mark.asyncio
async def test_owner_ai_draft_is_persistent_versioned_and_validated(
    session_factory, settings, encryption_key
) -> None:
    async with session_factory() as session:
        service = OwnerClientDraftService(
            session,
            FakeDraftProvider(),  # type: ignore[arg-type]
            EncryptionService(encryption_key),
            "test-model",
        )
        draft = await service.create(settings.platform_owner_telegram_id, "Создай Northwind")
        assert draft.draft_json["timezone"] == "Europe/Moscow"
        assert b"Northwind" not in draft.raw_prompt_ciphertext
        draft = await service.correct(
            settings.platform_owner_telegram_id, draft.id, "Поставь SLA 30 минут"
        )
        assert draft.version == 2
        assert draft.draft_json["response_sla_minutes"] == 30
        assert draft.manual_changes_json[-1]["changes"]["response_sla_minutes"]["to"] == 30


@pytest.mark.asyncio
async def test_owner_ai_draft_preserves_confirmed_identity_without_username(
    session_factory, settings, encryption_key
) -> None:
    identity = {
        "name": "TEST Project",
        "owner_name": "Вадим",
        "owner_telegram_user_id": 835691584,
        "owner_telegram_username": None,
    }
    async with session_factory() as session:
        service = OwnerClientDraftService(
            session,
            FakeDraftProvider(),  # type: ignore[arg-type]
            EncryptionService(encryption_key),
            "test-model",
        )
        draft = await service.create(
            settings.platform_owner_telegram_id,
            "Компания занимается автоматизацией продаж",
            identity=identity,
        )
        assert {key: draft.draft_json[key] for key in identity} == identity
        corrected = await service.correct(
            settings.platform_owner_telegram_id, draft.id, "Поставь SLA 30 минут"
        )
        assert {key: corrected.draft_json[key] for key in identity} == identity


@pytest.mark.asyncio
async def test_owner_ai_draft_rejects_secrets_before_provider_call(
    session_factory, settings, encryption_key
) -> None:
    provider = FakeDraftProvider()
    async with session_factory() as session:
        service = OwnerClientDraftService(
            session,
            provider,  # type: ignore[arg-type]
            EncryptionService(encryption_key),
            "test-model",
        )
        with pytest.raises(ValueError, match="содержит секрет"):
            await service.create(
                settings.platform_owner_telegram_id,
                "token " + "1234567890:" + "a" * 30,
            )


@pytest.mark.asyncio
async def test_owner_system_secret_is_encrypted_masked_and_loaded_after_restart(
    session_factory, settings, encryption_key
) -> None:
    plaintext = "sk-new-production-value-123456"
    async with session_factory() as session:
        service = SystemSecretService(session, EncryptionService(encryption_key))
        staged = await service.stage("deepseek_api_key", plaintext)
        assert plaintext.encode() not in staged.ciphertext
        await service.confirm("deepseek_api_key", staged.id)
        assert await service.get("deepseek_api_key") == plaintext
    resolved = await load_runtime_secret_overrides(session_factory, settings)
    assert resolved.deepseek_api_key.get_secret_value() == plaintext
    assert plaintext not in mask_secret(plaintext)
