from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.local_signals import LocalSignalEngine
from services.backend.jobs.queue import JobLease, SQLiteJobQueue
from services.backend.models import (
    BackgroundJob,
    MonitoredSource,
    TelegramConnection,
    TelegramMessage,
)
from services.backend.services.client_drafts import OwnerClientDraftService
from services.backend.services.encryption import EncryptionService
from services.backend.telegram_sessions.event_ingestion import TelegramEventIngestion


def event_lease(tenant_id: str, connection_id: str, peer_id: str, source_type: str) -> JobLease:
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
