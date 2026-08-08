from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..intelligence.signals import SignalService
from ..jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from ..models import (
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramIncrementalCursor,
    TelegramMessage,
)
from ..services.encryption import EncryptionService
from .gateway import RemoteMessage, TelegramFloodWait, TelegramUserGateway
from .service import TelegramConnectionService


class IncrementalTelegramIngestion:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        gateway: TelegramUserGateway,
        queue: SQLiteJobQueue,
        *,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.queue = queue
        self.transactions = SQLiteTransactionManager(session_factory)
        self.connections = TelegramConnectionService(session_factory, encryption, gateway)
        self.signals = SignalService(session_factory, queue)
        self.batch_size = batch_size

    async def fetch_updates(self, job: JobLease) -> dict[str, int]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        if job.telegram_account_id is None:
            return await self._fan_out(job)
        connection, session_string = await self.connections.connection_session(
            job.tenant_id, job.telegram_account_id
        )
        async with self.session_factory() as session:
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.tenant_id == job.tenant_id,
                        TelegramDialog.connection_id == connection.id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            )
        total_messages = 0
        all_signal_ids: list[str] = []
        newest_event: datetime | None = None
        try:
            for dialog in dialogs:
                cursor = await self._cursor(job.tenant_id, connection.id, dialog.id)
                batch = await self.gateway.fetch_new_messages(
                    session_string,
                    dialog.telegram_dialog_id,
                    after_id=cursor.last_message_id,
                    limit=self.batch_size,
                )
                message_count, signal_ids, last_event = await self._store_batch(
                    connection, dialog, cursor.id, batch.messages
                )
                total_messages += message_count
                all_signal_ids.extend(signal_ids)
                if last_event and (newest_event is None or last_event > newest_event):
                    newest_event = last_event
                if batch.has_more:
                    await self.queue.enqueue(
                        "telegram.fetch_updates",
                        {},
                        tenant_id=job.tenant_id,
                        telegram_account_id=connection.id,
                        priority=JOB_PRIORITY["P1"],
                        idempotency_key=f"telegram-fetch:{connection.id}:{dialog.id}:{batch.next_offset_id}",
                        correlation_id=job.correlation_id or job.id,
                        is_heavy=False,
                        category="telegram",
                        cost_class="light",
                    )
        except TelegramFloodWait:
            await self._mark_connection_error(connection.id, "flood_wait")
            raise
        finally:
            session_string = ""
        await self._mark_connection_complete(connection.id, newest_event)
        signals = await self._load_signals(all_signal_ids)
        await self.signals.enqueue_triage(signals)
        return {"connections": 1, "messages": total_messages, "signals": len(all_signal_ids)}

    async def _fan_out(self, job: JobLease) -> dict[str, int]:
        async with self.session_factory() as session:
            connections = list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == job.tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.session_secret_id.is_not(None),
                        TelegramConnection.status.in_(("connected", "ready")),
                    )
                )
            )
        for connection in connections:
            await self.queue.enqueue(
                "telegram.fetch_updates",
                {},
                tenant_id=job.tenant_id,
                telegram_account_id=connection.id,
                priority=job.attempts + JOB_PRIORITY["P1"],
                idempotency_key=f"telegram-fetch:{connection.id}:{job.id}",
                correlation_id=job.correlation_id or job.id,
                is_heavy=False,
                category="telegram",
                cost_class="light",
            )
        return {"connections": len(connections), "messages": 0, "signals": 0}

    async def _cursor(
        self, tenant_id: str, connection_id: str, dialog_id: str
    ) -> TelegramIncrementalCursor:
        async def write(session: AsyncSession) -> str:
            cursor = await session.scalar(
                select(TelegramIncrementalCursor).where(
                    TelegramIncrementalCursor.connection_id == connection_id,
                    TelegramIncrementalCursor.dialog_id == dialog_id,
                )
            )
            if cursor is None:
                last_message_id = int(
                    await session.scalar(
                        select(TelegramDialog.last_message_id).where(TelegramDialog.id == dialog_id)
                    )
                    or 0
                )
                cursor = TelegramIncrementalCursor(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    dialog_id=dialog_id,
                    last_message_id=last_message_id,
                )
                session.add(cursor)
                await session.flush()
            cursor.status = "running"
            cursor.last_error_code = None
            return cursor.id

        cursor_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            return await session.get(TelegramIncrementalCursor, cursor_id)

    async def _store_batch(
        self,
        connection: TelegramConnection,
        dialog: TelegramDialog,
        cursor_id: str,
        remote_messages: list[RemoteMessage],
    ) -> tuple[int, list[str], datetime | None]:
        signal_ids: list[str] = []
        inserted = 0
        last_event: datetime | None = None

        async def write(session: AsyncSession) -> None:
            nonlocal inserted, last_event
            cursor = await session.get(TelegramIncrementalCursor, cursor_id)
            current_dialog = await session.get(TelegramDialog, dialog.id)
            ordered = sorted(remote_messages, key=lambda item: item.id)
            for remote in ordered:
                existing = await session.scalar(
                    select(TelegramMessage).where(
                        TelegramMessage.dialog_id == dialog.id,
                        TelegramMessage.telegram_message_id == remote.id,
                    )
                )
                if existing is not None:
                    cursor.last_message_id = max(cursor.last_message_id, int(remote.id))
                    continue
                previous = list(
                    await session.scalars(
                        select(TelegramMessage)
                        .where(TelegramMessage.dialog_id == dialog.id)
                        .order_by(TelegramMessage.sent_at.desc())
                        .limit(10)
                    )
                )
                previous.reverse()
                message = TelegramMessage(
                    tenant_id=connection.tenant_id,
                    connection_id=connection.id,
                    dialog_id=dialog.id,
                    telegram_message_id=remote.id,
                    sender_id=remote.sender_id,
                    sender_username=remote.sender_username,
                    sent_at=remote.sent_at,
                    edited_at=remote.edited_at,
                    outgoing=remote.outgoing,
                    body_text=remote.text,
                    attachments_json=remote.attachments,
                )
                session.add(message)
                await session.flush()
                created = await self.signals.scan_message(session, message, previous)
                signal_ids.extend(item.id for item in created)
                inserted += 1
                cursor.last_message_id = max(cursor.last_message_id, int(remote.id))
                last_event = remote.sent_at
            now = datetime.now(UTC)
            cursor.status = "idle"
            cursor.last_sync_at = now
            cursor.last_event_at = last_event
            current_dialog.last_message_id = cursor.last_message_id
            current_dialog.last_sync_at = now
            if last_event:
                current_dialog.last_message_at = last_event

        await self.transactions.run(write)
        return inserted, signal_ids, last_event

    async def _load_signals(self, ids: list[str]) -> list[Signal]:
        if not ids:
            return []
        async with self.session_factory() as session:
            return list(await session.scalars(select(Signal).where(Signal.id.in_(ids))))

    async def _mark_connection_error(self, connection_id: str, code: str) -> None:
        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, connection_id)
            connection.error_state = code
            connection.health_status = "degraded"

        await self.transactions.run(write)

    async def _mark_connection_complete(
        self, connection_id: str, last_event: datetime | None
    ) -> None:
        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, connection_id)
            connection.last_incremental_sync_at = datetime.now(UTC)
            connection.last_event_at = last_event or connection.last_event_at
            connection.error_state = None

        await self.transactions.run(write)
