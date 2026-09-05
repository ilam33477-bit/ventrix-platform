from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from services.backend.api import client_router
from services.backend.api.client_router import (
    ClientAuthContext,
    ProblemReply,
    problem_conversation,
    reply_to_problem,
)
from services.backend.database import SQLiteTransactionManager
from services.backend.intelligence.notifications import NotificationDispatcher
from services.backend.intelligence.signals import SignalService
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.models import (
    BackgroundJob,
    DialogState,
    Employee,
    EncryptedSecret,
    NotificationLog,
    OperationalProblem,
    OutboundTelegramMessage,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantMembership,
)
from services.backend.services.employee_access import sync_employee_membership
from services.backend.telegram_sessions.runtime import TelegramSessionActor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["none", "reassign", "revoke", "disable", "cancelled", "delivery_uncertain"]
)
async def test_queued_employee_card_revalidates_recipient(
    session_factory, make_service, tenant_payload, change
):
    tenant, _, _, problem, _ = await reply_fixture(session_factory, make_service, tenant_payload)
    async with session_factory() as session:
        employee = Employee(tenant_id=tenant.id, display_name="Сотрудник", telegram_user_id=12345)
        session.add(employee)
        await session.flush()
        membership = await sync_employee_membership(session, employee)
        stored = await session.get(OperationalProblem, problem.id)
        stored.responsible_employee_id = employee.id
        log = NotificationLog(
            tenant_id=tenant.id,
            employee_id=employee.id,
            problem_id=problem.id,
            destination_type="employee",
            destination_id="12345",
            criticality=90,
            deduplication_key="test-queued-recipient",
            payload_json={"text": "Личная карточка"},
        )
        session.add(log)
        await session.flush()
        if change == "reassign":
            stored.responsible_employee_id = None
        elif change == "revoke":
            membership.status = "inactive"
        elif change == "disable":
            employee.notifications_enabled = False
        elif change in {"cancelled", "delivery_uncertain"}:
            log.status = change
        await session.commit()
    sender = SimpleNamespace(send=AsyncMock())
    dispatcher = NotificationDispatcher(session_factory, sender)
    job = SimpleNamespace(payload={"notification_id": log.id}, tenant_id=tenant.id)
    result = await dispatcher.dispatch(job)
    if change == "none":
        assert result["status"] == "sent"
        sender.send.assert_awaited_once()
    else:
        assert result["status"] in {"cancelled", "delivery_uncertain"}
        sender.send.assert_not_awaited()
    await dispatcher.dispatch(job)
    assert sender.send.await_count == (1 if change == "none" else 0)


@pytest.mark.asyncio
async def test_employee_cannot_open_or_reply_to_other_employees_problem(
    session_factory, make_service, tenant_payload
):
    tenant, _, _, problem, _ = await reply_fixture(session_factory, make_service, tenant_payload)
    async with session_factory() as session:
        context = ClientAuthContext(
            tenant=tenant,
            bot=SimpleNamespace(id="test"),
            membership=SimpleNamespace(role="employee", employee_id="different-employee"),
            permissions=frozenset({"problems.read_own", "problems.manage_own"}),
            telegram_user={"id": 123},
        )
        with pytest.raises(HTTPException) as detail_error:
            await client_router.problem_detail(problem.id, context, session)
        assert detail_error.value.status_code == 404
        with pytest.raises(HTTPException) as conversation_error:
            await problem_conversation(problem.id, context, before=None, limit=30, session=session)
        assert conversation_error.value.status_code == 404
        with pytest.raises(HTTPException) as reply_error:
            await reply_to_problem(
                problem.id,
                ProblemReply(text="Нельзя отправить", client_request_id=uuid4()),
                context,
                session,
            )
        assert reply_error.value.status_code == 404


class SendClient:
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.calls = 0

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_input_entity(self, peer_id: int) -> int:
        return peer_id

    async def __call__(self, _request):
        self.calls += 1
        return SimpleNamespace(id=90210, date=datetime.now(UTC))


def actor_for(connection, client, queue, session_factory) -> TelegramSessionActor:
    actor = TelegramSessionActor.__new__(TelegramSessionActor)
    actor.connection = connection
    actor.client = client
    actor.queue = queue
    actor.transactions = SQLiteTransactionManager(session_factory)
    actor.rpc_lock = asyncio.Lock()
    return actor


async def reply_fixture(session_factory, make_service, tenant_payload):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant.id)
        )
        secret = EncryptedSecret(
            tenant_id=tenant.id,
            kind="telegram_session",
            ciphertext=b"encrypted-test-session",
            fingerprint=f"reply-{tenant.id}",
        )
        session.add(secret)
        await session.flush()
        connection = TelegramConnection(
            tenant_id=tenant.id,
            session_secret_id=secret.id,
            telegram_user_id=700_001,
            username="employee_account",
            display_name="Рабочий аккаунт",
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=800_001,
            title="Клиент",
            username="customer",
            dialog_type="personal",
            source="personal",
            classification="auto_personal",
            selected=True,
            excluded=False,
        )
        session.add(dialog)
        await session.flush()
        source = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            telegram_message_id=10,
            sender_id=800_001,
            sender_username="customer",
            sender_role="customer",
            sent_at=datetime.now(UTC) - timedelta(minutes=61),
            outgoing=False,
            body_text="Можно получить договор?",
            attachments_json=[],
        )
        session.add(source)
        await session.flush()
        problem = OperationalProblem(
            tenant_id=tenant.id,
            connection_id=connection.id,
            dialog_id=dialog.id,
            source_message_id=source.id,
            fingerprint=f"reply-problem-{tenant.id}",
            problem_type="client_without_answer",
            issue_family="UNANSWERED_REQUEST",
            priority="high",
            confidence=0.95,
            evidence=source.body_text,
            explanation="Клиент ждёт ответа дольше установленного времени.",
            recommended_action="Ответить клиенту.",
            occurred_at=source.sent_at,
        )
        session.add(problem)
        session.add(
            DialogState(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                awaiting_employee_since=source.sent_at,
                response_expected_message_id=source.id,
                next_sla_check_at=source.sent_at + timedelta(minutes=60),
                last_activity_at=source.sent_at,
                open_commitments_json=[],
                unresolved_questions_json=[],
            )
        )
        await session.commit()
        context = ClientAuthContext(
            tenant=tenant,
            bot=SimpleNamespace(id="test-bot"),
            membership=membership,
            permissions=frozenset(),
            telegram_user={"id": membership.telegram_user_id},
        )
        return tenant, connection, dialog, problem, context


def test_reply_payload_forbids_server_owned_routing_fields() -> None:
    with pytest.raises(ValidationError):
        ProblemReply.model_validate(
            {
                "text": "Ответ",
                "client_request_id": str(uuid4()),
                "connection_id": "injected-connection",
            }
        )


@pytest.mark.asyncio
async def test_problem_reply_is_tenant_scoped_idempotent_and_uses_actor_lifecycle(
    session_factory, make_service, tenant_payload, monkeypatch
) -> None:
    tenant, connection, _dialog, problem, context = await reply_fixture(
        session_factory, make_service, tenant_payload
    )

    async def ignore_product_event(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(client_router, "record_event", ignore_product_event)
    request_id = uuid4()
    payload = ProblemReply(
        text="Отправляю договор. Пожалуйста, сообщите, если останутся вопросы.",
        client_request_id=request_id,
    )
    async with session_factory() as session:
        first = await reply_to_problem(problem.id, payload, context, session)
        second = await reply_to_problem(problem.id, payload, context, session)
    assert first["id"] == second["id"]

    async with session_factory() as session:
        assert int(await session.scalar(select(func.count(OutboundTelegramMessage.id))) or 0) == 1
        assert (
            int(
                await session.scalar(
                    select(func.count(BackgroundJob.id)).where(
                        BackgroundJob.job_type == "telegram.send_message"
                    )
                )
                or 0
            )
            == 1
        )
        pending_conversation = await problem_conversation(
            problem.id, context, before=None, limit=30, session=session
        )
    assert pending_conversation["outbound_commands"] == [
        {
            "id": first["id"],
            "client_request_id": str(request_id),
            "text": payload.text,
            "status": "pending",
            "telegram_message_id": None,
        }
    ]

    queue = SQLiteJobQueue(session_factory)
    lease = await queue.claim_next(
        "telegram-actor:test", allowed_categories=frozenset({"telegram_rpc"})
    )
    assert lease is not None and lease.job_type == "telegram.send_message"
    client = SendClient()
    actor = actor_for(connection, client, queue, session_factory)
    sent = await actor._send_message_job(lease)
    repeated = await actor._send_message_job(lease)
    await queue.complete(lease)

    assert sent["telegram_message_id"] == 90210
    assert repeated["deduplicated"] is True
    assert client.calls == 1

    local_scan = await queue.claim_next(
        "reply-lifecycle", allowed_categories=frozenset({"realtime"})
    )
    assert local_scan is not None and local_scan.job_type == "signal.local_scan"
    await SignalService(session_factory, queue).local_scan_job(local_scan)
    await queue.complete(local_scan)

    async with session_factory() as session:
        command = await session.scalar(select(OutboundTelegramMessage))
        outgoing = await session.scalar(
            select(TelegramMessage).where(TelegramMessage.telegram_message_id == 90210)
        )
        state = await session.scalar(select(DialogState).where(DialogState.tenant_id == tenant.id))
        refreshed_problem = await session.get(OperationalProblem, problem.id)
    assert command.status == "sent" and command.telegram_message_id == 90210
    assert outgoing is not None and outgoing.outgoing is True
    assert state.response_expected_message_id is None and state.next_sla_check_at is None
    assert refreshed_problem.status == "auto_resolved"

    async with session_factory() as session:
        conversation = await problem_conversation(
            problem.id, context, before=None, limit=30, session=session
        )
    assert [item["telegram_message_id"] for item in conversation["messages"]] == [10, 90210]
    assert conversation["outbound_commands"][0]["status"] == "sent"


@pytest.mark.asyncio
async def test_problem_reply_blocks_cross_tenant_and_unavailable_session(
    session_factory, make_service, tenant_payload, monkeypatch
) -> None:
    _tenant, connection, _dialog, problem, context = await reply_fixture(
        session_factory, make_service, tenant_payload
    )
    second_payload = tenant_payload.model_copy(
        update={
            "name": "Second tenant",
            "owner_telegram_user_id": tenant_payload.owner_telegram_user_id + 1,
            "owner_telegram_username": "second_owner",
        }
    )
    async with session_factory() as session:
        second = await make_service(session).create_tenant(second_payload)
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.tenant_id == second.id)
        )
    other_context = ClientAuthContext(
        tenant=second,
        bot=SimpleNamespace(id="second-bot"),
        membership=membership,
        permissions=frozenset(),
        telegram_user={"id": membership.telegram_user_id},
    )
    payload = ProblemReply(text="Ответ", client_request_id=uuid4())
    async with session_factory() as session:
        with pytest.raises(HTTPException) as cross_tenant:
            await reply_to_problem(problem.id, payload, other_context, session)
    assert cross_tenant.value.status_code == 404

    async with session_factory() as session:
        stored_connection = await session.get(TelegramConnection, connection.id)
        stored_connection.status = "reauthorization_required"
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(HTTPException) as unavailable:
            await reply_to_problem(problem.id, payload, context, session)
    assert unavailable.value.status_code == 409
    assert "недоступна" in unavailable.value.detail
