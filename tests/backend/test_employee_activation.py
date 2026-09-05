import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from telethon import errors, types

from services.backend.client_bots.handlers import build_client_router
from services.backend.database import SQLiteTransactionManager
from services.backend.jobs.queue import JobDeferred
from services.backend.models import (
    BotInstance,
    Employee,
    OperationalProblem,
    TelegramDialog,
    TelegramMessage,
    TenantMembership,
)
from services.backend.services.employee_activation import (
    ACTIVATION_JOB,
    ACTIVATION_PARAMETER,
    enqueue_employee_activation,
    start_employee_bot,
)
from services.backend.services.encryption import EncryptionService
from services.backend.telegram_sessions.gateway import LoginChallenge, LoginResult
from services.backend.telegram_sessions.service import TelegramConnectionService


async def activation_fixture(session_factory, make_service, tenant_payload, encryption_key):
    gateway = SimpleNamespace(
        begin_login=AsyncMock(return_value=LoginChallenge("pending", "hash", "telegram_app")),
        complete_login=AsyncMock(
            return_value=LoginResult("connected", "session", 777, "employee", "Test")
        ),
        cancel_login=AsyncMock(),
    )
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        secret = service._secret(tenant.id, "telegram_bot_token", "dummy-test-token")
        session.add(secret)
        await session.flush()
        bot = BotInstance(
            tenant_id=tenant.id,
            secret_id=secret.id,
            telegram_bot_id=12345,
            username="project_bot",
            display_name="Test bot",
            verified_at=datetime.now(UTC),
            runtime_status="running",
        )
        session.add(bot)
        await session.commit()
    pending = await service.begin_login(tenant.id, "+79990001122")
    connection = await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    lease = await service.queue.claim_next(
        "test-actor",
        telegram_account_id=connection.id,
        allowed_categories=frozenset({"telegram_rpc"}),
    )
    assert lease is not None and lease.job_type == ACTIVATION_JOB
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_me.return_value = SimpleNamespace(id=777)
    client.get_entity.return_value = types.User(id=12345, access_hash=99, bot=True)
    actor = SimpleNamespace(
        connection=connection,
        client=client,
        rpc_lock=asyncio.Lock(),
        transactions=SQLiteTransactionManager(session_factory),
    )
    return tenant, bot, service, actor, lease


async def test_activation_is_queued_atomically_deduplicated_and_uses_verified_bot(
    session_factory, make_service, tenant_payload, encryption_key
):
    _, _, service, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    async with session_factory() as session:
        assert await enqueue_employee_activation(session, actor.connection) == lease.id
        await session.commit()
    result = await start_employee_bot(actor, lease)
    assert result["status"] == "start_requested"
    request = actor.client.await_args.args[0]
    assert request.bot.user_id == request.peer.user_id == 12345
    assert request.start_param == ACTIVATION_PARAMETER
    assert request.random_id == lease.payload["random_id"]
    await service.queue.complete(lease, result)
    await start_employee_bot(actor, lease)
    assert actor.client.await_count == 1
    async with session_factory() as session:
        member = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.employee_id == actor.connection.assigned_employee_id
            )
        )
        assert member.bot_started_at is None  # Only the received /start confirms bot activation.


@pytest.mark.parametrize(
    "change",
    ["revoked", "disabled_employee", "started", "bot_disabled", "wrong_tenant", "wrong_employee"],
)
async def test_activation_rechecks_access_before_sending(
    session_factory, make_service, tenant_payload, encryption_key, change
):
    _, bot, _, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    async with session_factory() as session:
        member = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.employee_id == actor.connection.assigned_employee_id
            )
        )
        if change == "revoked":
            member.status = "inactive"
        elif change == "disabled_employee":
            employee = await session.get(Employee, member.employee_id)
            employee.status = "inactive"
        elif change == "started":
            member.bot_started_at = datetime.now(UTC)
        elif change == "bot_disabled":
            stored_bot = await session.get(BotInstance, bot.id)
            stored_bot.enabled = False
        elif change == "wrong_tenant":
            from dataclasses import replace

            lease = replace(lease, tenant_id="foreign-tenant")
        else:
            lease.payload["employee_id"] = "another-employee"
        await session.commit()
    assert (await start_employee_bot(actor, lease))["status"] == "skipped"
    actor.client.assert_not_awaited()


@pytest.mark.parametrize("wrong_identity", ["bot", "session"])
async def test_activation_never_sends_to_reused_username_or_wrong_session(
    session_factory, make_service, tenant_payload, encryption_key, wrong_identity
):
    _, _, _, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    if wrong_identity == "bot":
        actor.client.get_entity.return_value = types.User(id=54321, access_hash=99, bot=True)
    else:
        actor.client.get_me.return_value = SimpleNamespace(id=999)
    assert (await start_employee_bot(actor, lease))["status"] == "requires_manual_start"
    actor.client.assert_not_awaited()


async def test_activation_retry_keeps_telegram_random_id(
    session_factory, make_service, tenant_payload, encryption_key
):
    _, _, _, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    actor.client.side_effect = [TimeoutError(), None]
    with pytest.raises(TimeoutError):
        await start_employee_bot(actor, lease)
    await start_employee_bot(actor, lease)
    ids = [call.args[0].random_id for call in actor.client.await_args_list]
    assert ids == [lease.payload["random_id"]] * 2


async def test_activation_rechecks_membership_after_network_resolution(
    session_factory, make_service, tenant_payload, encryption_key
):
    _, _, _, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )

    async def resolve_and_revoke(_username):
        async with session_factory() as session:
            member = await session.scalar(
                select(TenantMembership).where(
                    TenantMembership.employee_id == actor.connection.assigned_employee_id
                )
            )
            member.status = "inactive"
            await session.commit()
        return types.User(id=12345, access_hash=99, bot=True)

    actor.client.get_entity.side_effect = resolve_and_revoke
    assert (await start_employee_bot(actor, lease))["reason"] == "access_changed"
    actor.client.assert_not_awaited()


@pytest.mark.parametrize("failure", ["blocked", "flood", "runtime"])
async def test_activation_respects_block_and_telegram_wait(
    session_factory, make_service, tenant_payload, encryption_key, failure
):
    _, _, _, actor, lease = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    if failure == "blocked":
        actor.client.side_effect = errors.YouBlockedUserError(request=None)
        assert (await start_employee_bot(actor, lease))["reason"] == "bot_blocked"
    else:
        if failure == "runtime":
            actor.client.is_connected.return_value = False
        else:
            actor.client.side_effect = errors.FloodWaitError(request=None, capture=45)
        with pytest.raises(JobDeferred):
            await start_employee_bot(actor, lease)


async def test_first_start_shows_guide_once_and_allows_skipping(
    session_factory, make_service, tenant_payload, encryption_key
):
    tenant, bot, _, actor, _ = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url="https://example.test")
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == "start"
    )
    context = SimpleNamespace(
        tenant=tenant,
        tenant_id=tenant.id,
        bot_instance_id=bot.id,
        telegram_user_id=777,
        employee_id=actor.connection.assigned_employee_id,
        role="employee",
        reports_read_all=False,
    )
    message = SimpleNamespace(text=f"/start {ACTIVATION_PARAMETER}", answer=AsyncMock())
    await handler(message, context)
    await handler(message, context)
    message.answer.assert_awaited_once()
    assert "Руководитель подключил ваш рабочий Telegram" in message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert any(b.text == "Пропустить обучение" for row in markup.inline_keyboard for b in row)
    assert any(
        b.web_app and b.text == "Открыть Ventrix AI" for row in markup.inline_keyboard for b in row
    )
    async with session_factory() as session:
        member = await session.scalar(
            select(TenantMembership).where(TenantMembership.employee_id == context.employee_id)
        )
        assert member.bot_started_at is not None


async def test_failed_welcome_can_be_retried(
    session_factory, make_service, tenant_payload, encryption_key
):
    tenant, bot, _, actor, _ = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url="https://example.test")
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == "start"
    )
    context = SimpleNamespace(
        tenant=tenant,
        tenant_id=tenant.id,
        bot_instance_id=bot.id,
        telegram_user_id=777,
        employee_id=actor.connection.assigned_employee_id,
        role="employee",
        reports_read_all=False,
    )
    message = SimpleNamespace(
        text=f"/start {ACTIVATION_PARAMETER}", answer=AsyncMock(side_effect=[TimeoutError(), None])
    )
    with pytest.raises(TimeoutError):
        await handler(message, context)
    await handler(message, context)
    assert message.answer.await_count == 2


@pytest.mark.parametrize("own", [True, False])
async def test_first_problem_link_keeps_card_and_does_not_reveal_other_employees_problem(
    session_factory, make_service, tenant_payload, encryption_key, own
):
    tenant, bot, _, actor, _ = await activation_fixture(
        session_factory, make_service, tenant_payload, encryption_key
    )
    async with session_factory() as session:
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=actor.connection.id,
            telegram_dialog_id=456,
            title="Client",
            dialog_type="personal",
            source="personal",
        )
        session.add(dialog)
        await session.flush()
        source = TelegramMessage(
            tenant_id=tenant.id,
            connection_id=actor.connection.id,
            dialog_id=dialog.id,
            telegram_message_id=1,
            sent_at=datetime.now(UTC),
            outgoing=False,
            body_text="Когда будет договор?",
            ingestion_source="live",
        )
        session.add(source)
        await session.flush()
        problem = OperationalProblem(
            tenant_id=tenant.id,
            connection_id=actor.connection.id,
            dialog_id=dialog.id,
            source_message_id=source.id,
            fingerprint="test-link",
            problem_type="client_without_answer",
            confidence=0.95,
            responsible_employee_id=actor.connection.assigned_employee_id if own else None,
            evidence=source.body_text,
            explanation="Нужен ответ",
            recommended_action="Ответить",
            occurred_at=source.sent_at,
        )
        session.add(problem)
        await session.commit()
    events = SimpleNamespace(session_factory=session_factory, record=AsyncMock())
    router = build_client_router(events, mini_app_url="https://example.test")
    handler = next(
        item.callback for item in router.message.handlers if item.callback.__name__ == "start"
    )
    context = SimpleNamespace(
        tenant=tenant,
        tenant_id=tenant.id,
        bot_instance_id=bot.id,
        telegram_user_id=777,
        employee_id=actor.connection.assigned_employee_id,
        role="employee",
        reports_read_all=False,
    )
    message = SimpleNamespace(text=f"/start problem_{problem.id}", answer=AsyncMock())
    await handler(message, context)
    message.answer.assert_awaited_once()
    body = message.answer.await_args.args[0]
    buttons = [
        b for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    if own:
        assert "Когда будет договор?" in body
        assert any(b.web_app and f"problem_id={problem.id}" in b.web_app.url for b in buttons)
        assert any(b.callback_data == "client:employee-guide:1" for b in buttons)
    else:
        assert "Когда будет договор?" not in body
        assert "недоступна" in body
