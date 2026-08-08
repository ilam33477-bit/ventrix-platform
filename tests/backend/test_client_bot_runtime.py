from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MenuButtonCommands
from sqlalchemy import select

from services.backend.client_bots.handlers import TenantOwnerMiddleware
from services.backend.client_bots.runtime import AiogramPollingRuntime, ClientBotRuntimeManager
from services.backend.models import BotInstance, EncryptedSecret, ProductEvent
from services.backend.schemas import BotCreate
from services.backend.services.encryption import EncryptionService
from services.backend.services.product_events import ProductEventService


class FakeRuntime:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.stopped = asyncio.Event()
        self.fail = fail

    async def run(self) -> None:
        if self.fail:
            raise self.fail
        await self.stopped.wait()

    async def stop(self) -> None:
        self.stopped.set()


class FakeFactory:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.runtimes: list[FakeRuntime] = []
        self.fail = fail

    def __call__(self, token: str, tenant_id: str, bot_id: str) -> FakeRuntime:
        self.calls.append((token, tenant_id, bot_id))
        runtime = FakeRuntime(self.fail)
        self.runtimes.append(runtime)
        return runtime


@pytest.mark.asyncio
async def test_runtime_synchronizes_web_app_menu_button() -> None:
    runtime = object.__new__(AiogramPollingRuntime)
    runtime.tenant_id = "tenant-1"
    runtime.mini_app_url = "https://ventrix.example.app"
    runtime.bot = SimpleNamespace(
        get_chat_menu_button=AsyncMock(return_value=MenuButtonCommands()),
        set_chat_menu_button=AsyncMock(),
    )

    await runtime.sync_menu_button()

    runtime.bot.set_chat_menu_button.assert_awaited_once()
    configured = runtime.bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert configured.text == "Ventrix AI"
    assert configured.web_app.url == runtime.mini_app_url


async def add_bots(session_factory, make_service, tenant_payload, encryption_key, count: int):
    async with session_factory() as session:
        service = make_service(session)
        tenant = await service.create_tenant(tenant_payload)
        first = await service.create_bot(tenant.id, BotCreate(token="mock-token-value-long-enough"))
        bots = [first]
        for index in range(1, count):
            token = f"mock-token-{index}-value-long-enough"
            secret = EncryptedSecret(
                tenant_id=tenant.id,
                kind="telegram_bot_token",
                ciphertext=EncryptionService(encryption_key).encrypt(token),
                fingerprint=EncryptionService(encryption_key).fingerprint(token),
            )
            session.add(secret)
            await session.flush()
            bot = BotInstance(
                tenant_id=tenant.id,
                secret_id=secret.id,
                telegram_bot_id=987654321 + index,
                username=f"axiom_ops_{index}_bot",
                display_name=f"Axiom {index}",
                verified_at=datetime.now(UTC),
            )
            session.add(bot)
            bots.append(bot)
        await session.commit()
        return tenant.id, [bot.id for bot in bots]


def manager(session_factory, encryption_key, factory, backoff=0.01):
    return ClientBotRuntimeManager(
        session_factory,
        EncryptionService(encryption_key),
        factory,
        sync_interval_seconds=0.01,
        heartbeat_seconds=0.01,
        restart_backoff_seconds=backoff,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3])
async def test_runtime_starts_one_or_multiple_bots(
    session_factory, make_service, tenant_payload, encryption_key, count
) -> None:
    _, bot_ids = await add_bots(
        session_factory, make_service, tenant_payload, encryption_key, count
    )
    factory = FakeFactory()
    runtime = manager(session_factory, encryption_key, factory)
    await runtime.sync_once()
    assert set(runtime.handles) == set(bot_ids)
    assert len(factory.calls) == count
    await runtime.stop()


@pytest.mark.asyncio
async def test_dynamic_start_stop_restart_and_recovery(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant_id, bot_ids = await add_bots(
        session_factory, make_service, tenant_payload, encryption_key, 1
    )
    factory = FakeFactory()
    runtime = manager(session_factory, encryption_key, factory)
    await runtime.sync_once()
    first_runtime = factory.runtimes[-1]

    async with session_factory() as session:
        first = await session.get(BotInstance, bot_ids[0])
        first.runtime_generation += 1
        await session.commit()
    await runtime.sync_once()
    assert first_runtime.stopped.is_set()
    assert len(factory.calls) == 2

    async with session_factory() as session:
        first = await session.get(BotInstance, bot_ids[0])
        first.enabled = False
        await session.commit()
    await runtime.sync_once()
    assert bot_ids[0] not in runtime.handles

    async with session_factory() as session:
        first = await session.get(BotInstance, bot_ids[0])
        first.enabled = True
        first.runtime_status = "running"  # stale state left by a terminated manager process
        first.runtime_generation += 1
        await session.commit()
    recovered = manager(session_factory, encryption_key, FakeFactory())
    await recovered.sync_once()
    assert bot_ids[0] in recovered.handles

    async with session_factory() as session:
        token = "dynamic-new-bot-token-long-enough"
        crypto = EncryptionService(encryption_key)
        secret = EncryptedSecret(
            tenant_id=tenant_id,
            kind="telegram_bot_token",
            ciphertext=crypto.encrypt(token),
            fingerprint=crypto.fingerprint(token),
        )
        session.add(secret)
        await session.flush()
        second = BotInstance(
            tenant_id=tenant_id,
            secret_id=secret.id,
            telegram_bot_id=987654399,
            username="dynamic_axiom_bot",
            display_name="Dynamic",
            verified_at=datetime.now(UTC),
        )
        session.add(second)
        await session.commit()
        second_id = second.id
    await recovered.sync_once()
    assert second_id in recovered.handles
    await recovered.stop()


@pytest.mark.asyncio
async def test_invalid_token_failure_is_safe_and_token_is_not_logged(
    session_factory, make_service, tenant_payload, encryption_key, caplog
) -> None:
    _, bot_ids = await add_bots(session_factory, make_service, tenant_payload, encryption_key, 1)
    factory = FakeFactory(RuntimeError("revoked mock-token-value-long-enough"))
    runtime = manager(session_factory, encryption_key, factory, backoff=60)
    await runtime.sync_once()
    await asyncio.sleep(0)
    await runtime.sync_once()
    async with session_factory() as session:
        bot = await session.get(BotInstance, bot_ids[0])
        assert bot.runtime_status == "failed"
        assert "mock-token" not in (bot.last_error or "")
    assert "mock-token" not in caplog.text


@pytest.mark.asyncio
async def test_owner_authorization_tenant_isolation_events_and_stats(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant_id, bot_ids = await add_bots(
        session_factory, make_service, tenant_payload, encryption_key, 1
    )
    events = ProductEventService(session_factory)
    middleware = TenantOwnerMiddleware(
        session_factory, events, tenant_id=tenant_id, bot_instance_id=bot_ids[0]
    )
    handler = AsyncMock(return_value="allowed")
    owner = SimpleNamespace(id=tenant_payload.owner_telegram_user_id)
    data = {"event_from_user": owner}
    assert await middleware(handler, object(), data) == "allowed"
    assert data["client_context"].tenant_id == tenant_id
    assert data["client_context"].role == "tenant_owner"

    stranger_handler = AsyncMock()
    await middleware(stranger_handler, object(), {"event_from_user": SimpleNamespace(id=999999)})
    stranger_handler.assert_not_awaited()
    await events.record(
        tenant_id=tenant_id,
        bot_instance_id=bot_ids[0],
        telegram_user_id=owner.id,
        event_name="summary_opened",
    )
    await events.record(
        tenant_id=tenant_id,
        bot_instance_id=bot_ids[0],
        telegram_user_id=owner.id,
        event_name="summary_opened",
    )
    await events.record(
        tenant_id=tenant_id,
        bot_instance_id=bot_ids[0],
        telegram_user_id=owner.id,
        event_name="support_opened",
    )
    stats = await events.stats(bot_ids[0], tenant_id)
    assert stats.unique_users == 2
    assert stats.total_events == 4
    assert stats.popular_buttons[0] == ("summary_opened", 2)

    with pytest.raises(LookupError):
        await events.record(
            tenant_id="00000000-0000-0000-0000-000000000000",
            bot_instance_id=bot_ids[0],
            event_name="summary_opened",
        )
    async with session_factory() as session:
        assert await session.scalar(
            select(ProductEvent).where(ProductEvent.event_name == "unauthorized_access_attempt")
        )
