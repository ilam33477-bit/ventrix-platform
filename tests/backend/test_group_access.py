from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    User,
)
from sqlalchemy import select

from services.backend.client_bots.handlers import TenantOwnerMiddleware, build_client_router
from services.backend.intelligence.notifications import (
    NotificationDispatcher,
    TelegramBotNotificationSender,
    TelegramRecipientUnavailable,
)
from services.backend.jobs.queue import JobDeferred
from services.backend.models import Employee, GroupIntegration, NotificationLog, TenantMembership
from services.backend.services.encryption import EncryptionService
from services.backend.services.group_access import observe_group


async def setup_group(session_factory, make_service, tenant_payload, make_group_bot):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        bot = await make_group_bot(session, tenant)
        await session.commit()
    events = SimpleNamespace(
        session_factory=session_factory, record=AsyncMock(), touch_update=AsyncMock()
    )
    router = build_client_router(events, mini_app_url="https://mini.example")
    client = AsyncMock()
    client.id = bot.telegram_bot_id
    client.get_chat_member.return_value = SimpleNamespace(status="administrator")
    client.get_chat_member_count.return_value = 3
    client.get_me.return_value = SimpleNamespace(username=bot.username)
    middleware = TenantOwnerMiddleware(
        session_factory, events, tenant_id=tenant.id, bot_instance_id=bot.id
    )
    return tenant, bot, router, client, middleware


@pytest.mark.parametrize("role", ["owner", "employee", "observer", "stranger"])
async def test_group_connect_requires_real_project_management(
    session_factory, make_service, tenant_payload, make_group_bot, role
):
    tenant, bot, router, client, middleware = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    user_id = tenant.owner_telegram_user_id if role == "owner" else 555
    if role in {"employee", "observer"}:
        async with session_factory() as session:
            employee = Employee(tenant_id=tenant.id, display_name="Tester", telegram_user_id=user_id)
            session.add(employee)
            await session.flush()
            session.add(
                TenantMembership(
                    tenant_id=tenant.id, telegram_user_id=user_id, role=role,
                    employee_id=employee.id, status="active"
                )
            )
            await session.commit()
    user = User(id=user_id, is_bot=False, first_name="Tester")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-10055, type="supergroup", title="TEST"),
        from_user=user,
        text="/ventrix_connect",
    ).as_(client)
    connect = next(
        h.callback for h in router.message.handlers if h.callback.__name__ == "connect_group"
    )

    async def route(event, data):
        await connect(event, data["client_context"])

    await middleware(route, message, {"event_from_user": user})
    async with session_factory() as session:
        group = await session.scalar(
            select(GroupIntegration).where(GroupIntegration.tenant_id == tenant.id)
        )
        if role == "owner":
            assert group.status == "active" and group.approved_by_telegram_user_id == user_id
            markup = client.await_args.args[0].reply_markup
            assert (
                markup.inline_keyboard[0][0].url
                == f"https://t.me/{bot.username}?start=group_connected"
            )
        else:
            assert group is None


async def test_unaffiliated_group_admin_addition_only_discovers_group(
    session_factory, make_service, tenant_payload, make_group_bot
):
    tenant, bot, router, client, middleware = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    outsider = User(id=444, is_bot=False, first_name="Stranger")
    bot_user = User(id=bot.telegram_bot_id, is_bot=True, first_name="Bot")
    event = ChatMemberUpdated.model_construct(
        chat=Chat(id=-10055, type="supergroup", title="TEST"),
        from_user=outsider,
        date=datetime.now(UTC),
        old_chat_member=ChatMemberMember(user=bot_user),
        new_chat_member=ChatMemberAdministrator.model_construct(
            user=bot_user, status="administrator"
        ),
    ).as_(client)
    observe = next(
        h.callback
        for h in router.my_chat_member.handlers
        if h.callback.__name__ == "group_membership"
    )

    async def route(event, data):
        assert data["client_context"].role == "observer"
        await observe(event, data["client_context"])

    await middleware(route, event, {"event_from_user": outsider})
    async with session_factory() as session:
        group = await session.scalar(
            select(GroupIntegration).where(GroupIntegration.tenant_id == tenant.id)
        )
        assert group.status == "pending" and group.approved_at is None


@pytest.mark.parametrize("transition", ["removed", "disabled"])
async def test_role_changes_cannot_restore_approval_or_disabled_group(
    session_factory, make_service, tenant_payload, make_group_bot, transition
):
    tenant, bot, _, _, _ = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    async with session_factory() as session:
        args = {
            "tenant_id": tenant.id,
            "bot_instance_id": bot.id,
            "chat_id": -10055,
            "title": "TEST",
        }
        group = await observe_group(
            session,
            **args,
            bot_status="administrator",
            approver_user_id=tenant.owner_telegram_user_id,
        )
        if transition == "removed":
            await observe_group(session, **args, bot_status="left")
        else:
            group.status = "disabled"
        await observe_group(session, **args, bot_status="administrator")
        assert group.status == ("pending" if transition == "removed" else "disabled")
        if transition == "removed":
            assert group.approved_at is None


async def test_project_menu_cannot_be_posted_to_group_even_by_owner(
    session_factory, make_service, tenant_payload, make_group_bot
):
    tenant, _, _, client, middleware = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    user = User(id=tenant.owner_telegram_user_id, is_bot=False, first_name="Owner")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-10055, type="supergroup"),
        from_user=user,
        text="/start",
    ).as_(client)
    handler = AsyncMock()
    await middleware(handler, message, {"event_from_user": user})
    handler.assert_not_awaited()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("correction", [False, True])
async def test_old_queued_reports_cannot_leak_personal_data_to_group(
    session_factory, make_service, tenant_payload, make_group_bot, approved, correction
):
    tenant, bot, _, _, _ = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    async with session_factory() as session:
        group = await observe_group(
            session,
            tenant_id=tenant.id,
            bot_instance_id=bot.id,
            chat_id=-10055,
            title="TEST",
            bot_status="administrator",
            approver_user_id=tenant.owner_telegram_user_id if approved else None,
        )
        # Includes legacy unsafe active state without proof of approval.
        group.status = "active"
        log = NotificationLog(
            tenant_id=tenant.id,
            group_integration_id=group.id,
            destination_type="group",
            destination_id="-10055",
            deduplication_key="legacy-report",
            criticality=0,
            payload_json={
                "text": "SECRET: personal employee metrics",
                "report_id": "test-report",
                "correction": correction,
                "reply_markup": {
                    "inline_keyboard": [[{"text": "Unsafe", "callback_data": "client:reports"}]]
                },
            },
        )
        session.add(log)
        await session.commit()
    sender = SimpleNamespace(send=AsyncMock())
    result = await NotificationDispatcher(session_factory, sender).dispatch(
        SimpleNamespace(tenant_id=tenant.id, payload={"notification_id": log.id})
    )
    if not approved:
        assert result["status"] == "cancelled"
        sender.send.assert_not_awaited()
    else:
        assert result["status"] == "sent"
        args = sender.send.await_args.args
        assert "SECRET" not in args[2]
        if correction:
            assert "не подтверждена" in args[2]
            assert "требует внимания" not in args[2]
        else:
            assert "Сводка проекта готова" in args[2]
        assert args[3]["inline_keyboard"][0][0] == {
            "text": "Открыть в Ventrix AI",
            "url": f"https://t.me/{bot.username}?start=report_test-report",
        }


@pytest.mark.parametrize("code", [200, 403, 429])
async def test_group_delivery_uses_bound_bot_and_handles_telegram_errors(
    session_factory, make_service, tenant_payload, make_group_bot, encryption_key, monkeypatch, code
):
    tenant, bot, _, _, _ = await setup_group(
        session_factory, make_service, tenant_payload, make_group_bot
    )
    async with session_factory() as session:
        group = await observe_group(
            session,
            tenant_id=tenant.id,
            bot_instance_id=bot.id,
            chat_id=-10055,
            title="TEST",
            bot_status="administrator",
            approver_user_id=tenant.owner_telegram_user_id,
        )
        await session.commit()
    requests = []
    async with session_factory() as session:
        newer_bot = await make_group_bot(session, tenant)
        await session.commit()

    def respond(request):
        requests.append(request)
        return httpx.Response(
            code, json={"ok": code == 200, "error_code": code, "parameters": {"retry_after": 45}}
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.backend.intelligence.notifications.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(respond), **kw),
    )
    sender = TelegramBotNotificationSender(
        session_factory, EncryptionService(encryption_key), "https://telegram.invalid"
    )
    if code == 200:
        await sender.send(tenant.id, "-10055", "TEST")
    elif code == 403:
        with pytest.raises(TelegramRecipientUnavailable):
            await sender.send(tenant.id, "-10055", "TEST")
        async with session_factory() as session:
            stored = await session.get(GroupIntegration, group.id)
            assert stored.approved_at is None and stored.status == "revoked"
    else:
        with pytest.raises(JobDeferred) as caught:
            await sender.send(tenant.id, "-10055", "TEST")
        assert caught.value.delay_seconds == 45
    assert len(requests) == 1
    assert f"test-bot-token-{bot.username[5:-4]}" in requests[0].url.path
    assert f"test-bot-token-{newer_bot.username[5:-4]}" not in requests[0].url.path
