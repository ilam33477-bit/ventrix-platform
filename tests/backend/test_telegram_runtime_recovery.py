from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from telethon import errors

from services.backend.database import SQLiteTransactionManager
from services.backend.intelligence.signals import SignalService
from services.backend.jobs.queue import JobDeferred, SQLiteJobQueue
from services.backend.models import (
    BackgroundJob,
    MonitoredSource,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramIncrementalCursor,
    TelegramMessage,
)
from services.backend.telegram_sessions.event_ingestion import TelegramEventIngestion
from services.backend.telegram_sessions.runtime import TelegramSessionActor


class CatchUpClient:
    def __init__(self, dialogs: list[SimpleNamespace], messages: dict[int, list[SimpleNamespace]]):
        self.dialogs = dialogs
        self.messages = messages
        self.min_ids: dict[int, list[int]] = {}
        self.dialog_catalog_requests = 0

    async def iter_dialogs(self):
        self.dialog_catalog_requests += 1
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(self, input_entity, *, min_id: int, reverse: bool, limit: int):
        assert reverse is True
        remote_id = int(input_entity)
        self.min_ids.setdefault(remote_id, []).append(min_id)
        available = [item for item in self.messages.get(remote_id, []) if item.id > min_id]
        for message in available[:limit]:
            yield message


class PartiallyRateLimitedCatalogClient(CatchUpClient):
    async def iter_dialogs(self):
        self.dialog_catalog_requests += 1
        yield self.dialogs[0]
        raise errors.FloodWaitError(request=None, capture=30)


class ImmediatelyRateLimitedCatalogClient(CatchUpClient):
    async def iter_dialogs(self):
        self.dialog_catalog_requests += 1
        if False:
            yield None
        raise errors.FloodWaitError(request=None, capture=30)


def remote_dialog(remote_id: int, source_type: str) -> SimpleNamespace:
    entity = SimpleNamespace(
        username=f"user_{remote_id}",
        broadcast=source_type == "channel",
    )
    return SimpleNamespace(
        id=remote_id,
        input_entity=remote_id,
        entity=entity,
        is_user=source_type == "personal",
        is_channel=source_type == "channel",
        name=f"Dialog {remote_id}",
        message=SimpleNamespace(date=datetime.now(UTC)),
    )


def remote_messages(count: int, *, start: int = 1) -> list[SimpleNamespace]:
    now = datetime.now(UTC)
    return [
        SimpleNamespace(
            id=message_id,
            sender_id=900_000 + message_id,
            date=now + timedelta(milliseconds=message_id),
            edit_date=None,
            out=False,
            message=f"Message {message_id}",
            media=None,
        )
        for message_id in range(start, start + count)
    ]


def actor_for(connection, client, queue, session_factory) -> TelegramSessionActor:
    actor = TelegramSessionActor.__new__(TelegramSessionActor)
    actor.connection = connection
    actor.client = client
    actor.queue = queue
    actor.transactions = SQLiteTransactionManager(session_factory)
    actor.rpc_lock = asyncio.Lock()
    return actor


@pytest.mark.asyncio
async def test_live_group_filter_uses_only_explicit_monitored_sources(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.flush()
        session.add(
            MonitoredSource(
                tenant_id=tenant.id,
                connection_id=connection.id,
                canonical_peer_id="-100123",
                source_type="group",
                title="Opted in",
                added_via="test",
                enabled=True,
            )
        )
        await session.commit()

    actor = actor_for(connection, object(), SQLiteJobQueue(session_factory), session_factory)
    assert await actor._is_monitored_source("-100123")
    assert not await actor._is_monitored_source("-100999")


@pytest.mark.asyncio
async def test_catch_up_paginates_entire_1200_message_gap_without_duplicates(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(
            tenant_id=tenant.id,
            telegram_user_id=800_001,
            status="ready",
        )
        session.add(connection)
        await session.flush()
        dialog = TelegramDialog(
            tenant_id=tenant.id,
            connection_id=connection.id,
            telegram_dialog_id=1001,
            canonical_peer_id="1001",
            title="Existing personal",
            dialog_type="personal",
            source="personal",
            selected=True,
            excluded=False,
            last_message_id=10,
        )
        session.add(dialog)
        await session.flush()
        session.add(
            TelegramIncrementalCursor(
                tenant_id=tenant.id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                last_message_id=10,
            )
        )
        await session.commit()

    client = CatchUpClient(
        [remote_dialog(1001, "personal")],
        {1001: remote_messages(1200, start=11)},
    )
    queue = SQLiteJobQueue(session_factory)
    actor = actor_for(connection, client, queue, session_factory)
    first = await actor.catch_up()
    second = await actor.catch_up()
    assert client.dialog_catalog_requests == 1

    async with session_factory() as session:
        queued = int(
            await session.scalar(
                select(func.count(BackgroundJob.id)).where(
                    BackgroundJob.job_type == "telegram.ingest_event"
                )
            )
            or 0
        )
    assert first["events"] == 1200
    assert second["events"] == 0
    assert client.min_ids[1001] == [10, 510, 1010, 1210]
    assert queued == 1200
    async with session_factory() as session:
        cursor = await session.scalar(select(TelegramIncrementalCursor))
        assert cursor is not None and cursor.last_message_id == 1210


@pytest.mark.asyncio
async def test_partial_catalog_is_kept_without_retrying_more_rpc(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.commit()

    client = PartiallyRateLimitedCatalogClient(
        [remote_dialog(1001, "personal"), remote_dialog(1002, "personal")],
        {1001: remote_messages(1)},
    )
    actor = actor_for(connection, client, SQLiteJobQueue(session_factory), session_factory)

    async def ignore_rate_limit(_: int) -> None:
        return None

    actor._rate_limited = ignore_rate_limit
    result = await actor.catch_up()

    assert result == {"events": 0, "discovered_dialogs": 1}
    assert client.min_ids == {}
    assert set(actor._remote_catalog or {}) == {1001}


@pytest.mark.asyncio
async def test_empty_rate_limited_catalog_defers_to_next_reconciliation(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.commit()

    client = ImmediatelyRateLimitedCatalogClient([], {})
    actor = actor_for(connection, client, SQLiteJobQueue(session_factory), session_factory)

    async def ignore_rate_limit(_: int) -> None:
        return None

    actor._rate_limited = ignore_rate_limit
    assert await actor.catch_up() == {"events": 0, "discovered_dialogs": 0}


@pytest.mark.asyncio
async def test_catch_up_job_waits_for_connected_client() -> None:
    actor = TelegramSessionActor.__new__(TelegramSessionActor)
    actor.client = SimpleNamespace(is_connected=lambda: False)
    with pytest.raises(JobDeferred) as raised:
        await actor._catch_up_job(SimpleNamespace())
    assert raised.value.delay_seconds == 15


@pytest.mark.asyncio
async def test_catch_up_discovers_only_new_personal_dialog_and_uses_ingest_pipeline(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(
            tenant_id=tenant.id,
            telegram_user_id=800_002,
            status="ready",
        )
        session.add(connection)
        await session.commit()

    personal_messages = remote_messages(2)
    personal_messages[-1].message = "Клиент просит прислать договор сегодня."
    client = CatchUpClient(
        [remote_dialog(2001, "personal"), remote_dialog(3001, "group")],
        {
            2001: personal_messages,
            3001: remote_messages(2),
        },
    )
    queue = SQLiteJobQueue(session_factory)
    actor = actor_for(connection, client, queue, session_factory)
    result = await actor.catch_up()
    assert result == {"events": 2, "discovered_dialogs": 1}

    ingestion = TelegramEventIngestion(session_factory, queue)
    for _ in range(2):
        lease = await queue.claim_next(
            "recovery-ingest", allowed_categories=frozenset({"realtime"})
        )
        assert lease is not None and lease.job_type == "telegram.ingest_event"
        await ingestion.ingest(lease)
        await queue.complete(lease)

    signals = SignalService(session_factory, queue)
    for _ in range(2):
        lease = await queue.claim_next(
            "recovery-signals", allowed_categories=frozenset({"realtime"})
        )
        assert lease is not None and lease.job_type == "signal.scan_batch"
        await signals.scan_batch_job(lease)
        await queue.complete(lease)

    repeated = await actor.catch_up()

    async with session_factory() as session:
        dialogs = list(await session.scalars(select(TelegramDialog)))
        messages = list(await session.scalars(select(TelegramMessage)))
        cursor = await session.scalar(select(TelegramIncrementalCursor))
        signal_count = int(await session.scalar(select(func.count(Signal.id))) or 0)
        scan_jobs = int(
            await session.scalar(
                select(func.count(BackgroundJob.id)).where(
                    BackgroundJob.job_type == "signal.scan_batch"
                )
            )
            or 0
        )
    assert len(dialogs) == 1
    assert repeated == {"events": 0, "discovered_dialogs": 0}
    assert dialogs[0].telegram_dialog_id == 2001
    assert dialogs[0].tenant_id == tenant.id
    assert dialogs[0].connection_id == connection.id
    assert dialogs[0].dialog_type == "personal" and dialogs[0].selected is True
    assert [item.telegram_message_id for item in messages] == [1, 2]
    assert cursor.last_message_id == 2
    assert scan_jobs == 2
    assert signal_count >= 1
