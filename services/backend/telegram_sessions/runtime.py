from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient, errors, events, functions, utils

from ..config import get_settings
from ..database import SQLiteTransactionManager, get_session_factory
from ..jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from ..jobs.worker import BackgroundWorker
from ..models import (
    EncryptedSecret,
    MonitoredSource,
    OutboundTelegramMessage,
    TelegramConnection,
    TelegramDialog,
    TelegramFolder,
    TelegramIncrementalCursor,
    TelegramMessage,
    TenantSettings,
)
from ..observability import configure_structured_logging, log_event
from ..services.encryption import EncryptionService
from ..services.system_secrets import load_runtime_secret_overrides
from .gateway import MessageBatch, RemoteMessage, TelegramFloodWait, TelethonGateway
from .leases import RuntimeOwnership, TelegramRuntimeLeaseStore
from .service import TelegramConnectionService
from .sync import TelegramSyncHandlers

logger = logging.getLogger(__name__)
CATCH_UP_PAGE_SIZE = 500
CATCH_UP_CATALOG_TTL = timedelta(hours=1)


def is_automated_private_entity(entity: Any) -> bool:
    """Exclude Telegram bots, service/support users, deleted users and Saved Messages."""
    username = str(getattr(entity, "username", None) or "").lower()
    return bool(
        getattr(entity, "bot", False)
        or getattr(entity, "support", False)
        or getattr(entity, "deleted", False)
        or getattr(entity, "is_self", False)
        or username.endswith("bot")
    )


class TelegramSessionActor:
    """The sole lifecycle owner of one active Telethon session in this runtime."""

    def __init__(
        self,
        connection: TelegramConnection,
        session_string: str,
        gateway: TelethonGateway,
        queue: SQLiteJobQueue,
        session_factory: async_sessionmaker[AsyncSession],
        ownership: RuntimeOwnership,
        leases: TelegramRuntimeLeaseStore,
        sync_handlers: TelegramSyncHandlers,
    ) -> None:
        self.connection = connection
        self.session_string = session_string
        self.client: TelegramClient = gateway.client(session_string, receive_updates=True)
        self.queue = queue
        self.transactions = SQLiteTransactionManager(session_factory)
        self.rpc_lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self.ownership = ownership
        self.leases = leases
        self.sync_handlers = sync_handlers
        self._updates_received = 0
        self._edited_received = 0
        self._monitored_peer_ids: set[str] | None = None
        self._remote_catalog: dict[int, dict[str, Any]] | None = None
        self._catalog_refreshed_at: datetime | None = None

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    self._connection_loop(), name=f"telegram-link-{self.connection.id}"
                )
                tasks.create_task(self._command_loop(), name=f"telegram-rpc-{self.connection.id}")
                tasks.create_task(self._lease_loop(), name=f"telegram-lease-{self.connection.id}")
                tasks.create_task(
                    self._counter_loop(), name=f"telegram-counters-{self.connection.id}"
                )
        finally:
            await self._flush_counters()
            if self.client.is_connected():
                await self.client.disconnect()
            await self._health("disconnected", reconnects=self.connection.reconnect_count)
            await self.leases.release(self.ownership)

    async def _connection_loop(self) -> None:
        reconnects = 0
        self.client.add_event_handler(self._new_message, events.NewMessage())
        self.client.add_event_handler(self._edited_message, events.MessageEdited())
        while not self._stopping.is_set():
            try:
                await self.client.connect()
                await self._health("running", reconnects=reconnects)
                if not await self.queue.has_unfinished(
                    "telegram.catch_up", telegram_account_id=self.connection.id
                ):
                    await self.queue.enqueue(
                        "telegram.catch_up",
                        {},
                        tenant_id=self.connection.tenant_id,
                        telegram_account_id=self.connection.id,
                        priority=JOB_PRIORITY["P2"],
                        scheduled_at=datetime.now(UTC)
                        + timedelta(seconds=self._startup_catch_up_delay()),
                        idempotency_key=(
                            f"telegram-catchup:{self.connection.id}:{datetime.now(UTC):%Y%m%d%H%M}"
                        ),
                        category="telegram_rpc",
                        cost_class="light",
                    )
                await self.client.run_until_disconnected()
                if self._stopping.is_set():
                    break
                reconnects += 1
                await self._health("reconnecting", reconnects=reconnects)
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                await self._rate_limited(int(exc.seconds))
                await asyncio.sleep(max(1, int(exc.seconds)))
            except (
                errors.AuthKeyDuplicatedError,
                errors.AuthKeyUnregisteredError,
                errors.SessionRevokedError,
                errors.SessionExpiredError,
                errors.UserDeactivatedError,
            ) as exc:
                await self._fatal_auth(type(exc).__name__)
                self._stopping.set()
                break
            except Exception as exc:  # noqa: BLE001 - supervised reconnect boundary
                reconnects += 1
                await self._health("degraded", reconnects=reconnects, error=type(exc).__name__)
                backoff = min(60.0, 2 ** min(reconnects, 6))
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.2))

    async def _command_loop(self) -> None:
        worker = BackgroundWorker(
            self.queue,
            f"telegram-actor:{self.ownership.owner_instance_id}:{self.connection.id}",
            {
                "telegram.catch_up": self._catch_up_job,
                "telegram.preview_source": self._preview_source_job,
                "telegram.confirm_sources": self._confirm_sources_job,
                "telegram.health": self._health_job,
                "telegram.logout": self._logout_job,
                "telegram.sync_chat": self._sync_chat_job,
                "telegram.refresh_catalog": self._refresh_catalog_job,
                "telegram.prepare_connection": self._prepare_connection_job,
                "telegram.send_message": self._send_message_job,
            },
            allowed_categories=frozenset({"telegram_rpc"}),
            telegram_account_id=self.connection.id,
        )
        idle_delay = 0.25
        while not self._stopping.is_set():
            if await worker.run_once():
                idle_delay = 0.25
                await asyncio.sleep(0)
                continue
            await asyncio.sleep(idle_delay)
            idle_delay = min(2.0, idle_delay * 2)

    async def _lease_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(10)
            refreshed = await self.leases.heartbeat(self.ownership)
            if refreshed is None:
                await self._health("degraded", reconnects=0, error="runtime_lease_lost")
                self._stopping.set()
                if self.client.is_connected():
                    await self.client.disconnect()
                return
            self.ownership = refreshed

    async def _counter_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(10)
            await self._flush_counters()

    async def _catch_up_job(self, _: JobLease) -> dict[str, int]:
        return await self.catch_up()

    def _startup_catch_up_delay(self) -> int:
        """Spread reconnect recovery across accounts sharing one Telegram API app."""

        return int(hashlib.sha256(self.connection.id.encode()).hexdigest()[:4], 16) % 45

    async def _preview_source_job(self, job: JobLease) -> dict[str, Any]:
        return await self.preview_source(str(job.payload["link"]))

    async def _confirm_sources_job(self, job: JobLease) -> dict[str, Any]:
        result = await self.confirm_sources(
            dict(job.payload["preview"]),
            [str(item) for item in job.payload.get("selected_peer_ids", [])],
            bool(job.payload.get("join")),
        )
        if not result.get("requires_join"):
            result["history_events"] = (await self.catch_up())["events"]
        return result

    async def _health_job(self, _: JobLease) -> dict[str, Any]:
        authorized = await self.client.is_user_authorized()
        profile = await self.client.get_me() if authorized else None

        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, self.connection.id)
            if connection is None:
                return
            connection.last_health_check_at = datetime.now(UTC)
            connection.health_status = "healthy" if authorized else "revoked"
            if not authorized:
                connection.status = "reauthorization_required"
                connection.last_error_code = "session_revoked"
            elif profile is not None:
                connection.telegram_user_id = int(profile.id)
                connection.username = profile.username
                connection.display_name = (
                    " ".join(item for item in (profile.first_name, profile.last_name) if item)
                    or None
                )

        await self.transactions.run(write)
        return {"authorized": authorized, "status": "connected" if authorized else "revoked"}

    async def _logout_job(self, _: JobLease) -> dict[str, bool]:
        async with self.rpc_lock:
            await self.client.log_out()
        self._stopping.set()
        return {"logged_out": True}

    async def _send_message_job(self, job: JobLease) -> dict[str, Any]:
        command_id = str(job.payload["outbound_message_id"])

        async def start(session: AsyncSession) -> tuple[str, int, str, str] | None:
            command = await session.scalar(
                select(OutboundTelegramMessage).where(
                    OutboundTelegramMessage.id == command_id,
                    OutboundTelegramMessage.tenant_id == job.tenant_id,
                    OutboundTelegramMessage.connection_id == self.connection.id,
                )
            )
            if command is None:
                raise LookupError("outbound Telegram command not found")
            if command.status == "sent":
                return None
            dialog = await session.scalar(
                select(TelegramDialog).where(
                    TelegramDialog.id == command.dialog_id,
                    TelegramDialog.tenant_id == command.tenant_id,
                    TelegramDialog.connection_id == command.connection_id,
                    TelegramDialog.excluded.is_(False),
                )
            )
            if dialog is None:
                raise LookupError("outbound Telegram dialog not available")
            command.status = "sending"
            command.attempts += 1
            command.last_error_code = None
            return (
                command.text,
                command.telegram_random_id,
                command.dialog_id,
                str(dialog.telegram_dialog_id),
            )

        prepared = await self.transactions.run(start)
        if prepared is None:
            async with self.transactions.session_factory() as session:
                command = await session.get(OutboundTelegramMessage, command_id)
                return {
                    "outbound_message_id": command_id,
                    "telegram_message_id": command.telegram_message_id if command else None,
                    "deduplicated": True,
                }
        text, random_id, dialog_id, telegram_dialog_id = prepared
        try:
            async with self.rpc_lock:
                if not await self.client.is_user_authorized():
                    raise RuntimeError("telegram_session_unavailable")
                peer = await self.client.get_input_entity(int(telegram_dialog_id))
                result = await self.client(
                    functions.messages.SendMessageRequest(
                        peer=peer,
                        message=text,
                        random_id=random_id,
                        no_webpage=True,
                    )
                )
        except Exception as exc:
            error_code = type(exc).__name__

            async def fail(session: AsyncSession, code: str = error_code) -> None:
                command = await session.get(OutboundTelegramMessage, command_id)
                if command is not None and command.status != "sent":
                    command.status = "failed"
                    command.last_error_code = code

            await self.transactions.run(fail)
            raise

        telegram_message = self._sent_message(result)
        telegram_message_id = int(telegram_message.id) if telegram_message is not None else None
        sent_at = getattr(telegram_message, "date", None) or datetime.now(UTC)

        async def finish(session: AsyncSession) -> str | None:
            command = await session.get(OutboundTelegramMessage, command_id)
            if command is None:
                raise LookupError("outbound Telegram command disappeared")
            command.status = "sent"
            command.telegram_message_id = telegram_message_id
            command.sent_at = sent_at
            command.last_error_code = None
            if telegram_message_id is None:
                return None
            stored = await session.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.dialog_id == dialog_id,
                    TelegramMessage.telegram_message_id == telegram_message_id,
                )
            )
            if stored is None:
                stored = TelegramMessage(
                    tenant_id=command.tenant_id,
                    connection_id=command.connection_id,
                    dialog_id=dialog_id,
                    telegram_message_id=telegram_message_id,
                    sender_id=self.connection.telegram_user_id,
                    sender_username=self.connection.username,
                    sender_role="account_owner",
                    ingestion_source="mini_app_reply",
                    sent_at=sent_at,
                    outgoing=True,
                    body_text=text,
                    attachments_json=[],
                )
                session.add(stored)
                await session.flush()
            return stored.id

        stored_message_id = await self.transactions.run(finish)
        if stored_message_id is not None:
            await self.queue.enqueue(
                "signal.local_scan",
                {"message_id": stored_message_id},
                tenant_id=job.tenant_id,
                telegram_account_id=self.connection.id,
                dialog_id=dialog_id,
                category="realtime",
                cost_class="light",
                idempotency_key=f"mini-app-reply-scan:{command_id}",
            )
        return {
            "outbound_message_id": command_id,
            "telegram_message_id": telegram_message_id,
            "deduplicated": False,
        }

    @staticmethod
    def _sent_message(result: Any) -> Any | None:
        if getattr(result, "id", None) is not None:
            return result
        for update in getattr(result, "updates", ()):
            message = getattr(update, "message", None)
            if message is not None and getattr(message, "id", None) is not None:
                return message
        return None

    async def _sync_chat_job(self, job: JobLease) -> dict[str, object]:
        loaded = await self.sync_handlers.load_actor_context(
            str(job.payload["cursor_id"]), job.tenant_id
        )
        if loaded is None:
            return {"skipped": "cursor_missing"}
        cursor, run, connection, dialog = loaded
        if run.stop_requested or connection.stop_requested:
            await self.sync_handlers._mark_cursor(cursor.id, "stopped", "stop_requested")
            await self.sync_handlers._refresh_run(run.id)
            return {"stopped": True}
        messages: list[RemoteMessage] = []
        try:
            async with self.rpc_lock:
                async for item in self.client.iter_messages(
                    dialog.telegram_dialog_id,
                    limit=self.sync_handlers.batch_size,
                    offset_id=cursor.offset_message_id,
                ):
                    attachments: list[dict[str, str | int | None]] = []
                    if item.media:
                        file = getattr(item, "file", None)
                        attachments.append(
                            {
                                "kind": type(item.media).__name__,
                                "name": getattr(file, "name", None),
                                "size": getattr(file, "size", None),
                                "mime_type": getattr(file, "mime_type", None),
                            }
                        )
                    messages.append(
                        RemoteMessage(
                            int(item.id),
                            int(item.sender_id) if item.sender_id else None,
                            None,
                            item.date,
                            item.edit_date,
                            bool(item.out),
                            item.message or None,
                            attachments,
                        )
                    )
        except errors.FloodWaitError as exc:
            await self._rate_limited(int(exc.seconds))
            raise TelegramFloodWait(int(exc.seconds)) from None
        batch = MessageBatch(
            messages,
            min((item.id for item in messages), default=cursor.offset_message_id),
            len(messages) == self.sync_handlers.batch_size,
        )
        return await self.sync_handlers.process_actor_batch(
            job, cursor, run, connection, dialog, batch
        )

    async def _refresh_catalog_job(self, _: JobLease) -> dict[str, int]:
        async with self.rpc_lock:
            response = await self.client(functions.messages.GetDialogFiltersRequest())
            filters = getattr(response, "filters", response)
            folders = [
                (int(item.id), str(getattr(item, "title", "Рабочая папка")))
                for item in filters
                if getattr(item, "id", 0)
            ]
            dialogs: list[dict[str, Any]] = []
            async for dialog in self.client.iter_dialogs():
                entity = dialog.entity
                kind = (
                    "personal"
                    if dialog.is_user
                    else "channel"
                    if dialog.is_channel and getattr(entity, "broadcast", False)
                    else "group"
                )
                raw = getattr(dialog, "dialog", None)
                dialogs.append(
                    {
                        "id": int(dialog.id),
                        "title": str(dialog.name or "Без названия"),
                        "username": getattr(entity, "username", None),
                        "kind": kind,
                        "folder_id": getattr(raw, "folder_id", None),
                        "participants_count": getattr(entity, "participants_count", None),
                        "last_message_at": getattr(dialog.message, "date", None),
                        "is_automated": bool(
                            kind == "personal" and is_automated_private_entity(entity)
                        ),
                    }
                )

        async def write(session: AsyncSession) -> None:
            tenant_settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == self.connection.tenant_id)
            )
            active_days = tenant_settings.active_dialog_days if tenant_settings else 30
            active_cutoff = datetime.now(UTC) - timedelta(days=active_days)
            await session.execute(
                TelegramFolder.__table__.delete().where(
                    TelegramFolder.connection_id == self.connection.id
                )
            )
            for folder_id, title in folders:
                session.add(
                    TelegramFolder(
                        tenant_id=self.connection.tenant_id,
                        connection_id=self.connection.id,
                        telegram_folder_id=folder_id,
                        title=title,
                        chat_count=sum(item["folder_id"] == folder_id for item in dialogs),
                    )
                )
            existing = {
                row.telegram_dialog_id: row
                for row in await session.scalars(
                    select(TelegramDialog).where(TelegramDialog.connection_id == self.connection.id)
                )
            }
            for remote in dialogs:
                row = existing.get(remote["id"])
                if row is None:
                    row = TelegramDialog(
                        tenant_id=self.connection.tenant_id,
                        connection_id=self.connection.id,
                        telegram_dialog_id=remote["id"],
                        canonical_peer_id=str(remote["id"]),
                    )
                    session.add(row)
                row.title = remote["title"]
                row.username = remote["username"]
                row.dialog_type = remote["kind"]
                row.folder_id = remote["folder_id"]
                row.participants_count = remote["participants_count"]
                row.last_message_at = remote["last_message_at"]
                row.source = "personal" if remote["kind"] == "personal" else "folder"
                if remote["kind"] == "personal":
                    row.classification = (
                        "automated_account" if remote["is_automated"] else "auto_personal"
                    )
                    row.confidence = 1.0
                    row.requires_user_confirmation = False
                    row.selected = bool(
                        not remote["is_automated"]
                        and remote["last_message_at"] is not None
                        and remote["last_message_at"] >= active_cutoff
                    )
                    row.excluded = remote["is_automated"]
            connection = await session.get(TelegramConnection, self.connection.id)
            connection.progress_stage = "catalog_ready"
            connection.progress_percent = 25
            connection.last_catchup_at = datetime.now(UTC)
            connection.progress_json = {
                "folders": len(folders),
                "dialogs": len(dialogs),
                "personal_dialogs": sum(item["kind"] == "personal" for item in dialogs),
            }

        await self.transactions.run(write)
        return {"folders": len(folders), "dialogs": len(dialogs)}

    async def _prepare_connection_job(self, job: JobLease) -> dict[str, Any]:
        try:
            catalog = await self._refresh_catalog_job(job)
            service = TelegramConnectionService(
                self.sync_handlers.session_factory,
                self.sync_handlers.encryption,
                self.sync_handlers.gateway,
            )
            await service.activate_default_scope(
                self.connection.tenant_id,
                history_days=int(job.payload.get("history_days") or 14),
                connection_id=self.connection.id,
            )
            run = await service.start_initial_sync(
                self.connection.tenant_id,
                connection_id=self.connection.id,
            )
            return {**catalog, "analysis_run_id": run.id}
        except Exception:
            logger.exception(
                "Telegram connection preparation failed tenant_id=%s connection_id=%s job_id=%s",
                self.connection.tenant_id,
                self.connection.id,
                job.id,
            )
            raise

    async def stop(self) -> None:
        self._stopping.set()
        if self.client.is_connected():
            await self.client.disconnect()

    async def _new_message(self, event: Any) -> None:
        await self._enqueue_event(event, "new")

    async def _edited_message(self, event: Any) -> None:
        await self._enqueue_event(event, "edited")

    async def _enqueue_event(self, event: Any, event_type: str) -> None:
        message = event.message
        chat = event.chat
        source_type = (
            "personal"
            if event.is_private
            else "channel"
            if getattr(chat, "broadcast", False)
            else "group"
        )
        peer_id = str(event.chat_id)
        if source_type == "personal" and is_automated_private_entity(chat):
            return
        if source_type != "personal" and not await self._is_monitored_source(peer_id):
            # Telethon can replay a large difference for every group/channel on
            # reconnect. Only explicitly opted-in sources belong in the durable
            # application queue; dropping the rest here avoids thousands of
            # jobs that ingestion would immediately ignore anyway.
            return
        attachments: list[dict[str, Any]] = []
        if message.media:
            file = getattr(message, "file", None)
            attachments.append(
                {
                    "kind": type(message.media).__name__,
                    "name": getattr(file, "name", None),
                    "size": getattr(file, "size", None),
                    "mime_type": getattr(file, "mime_type", None),
                }
            )
        edited_at = getattr(message, "edit_date", None)
        event_version = edited_at.isoformat() if event_type == "edited" and edited_at else "new"
        partition_key = f"telegram-dialog:{self.connection.tenant_id}:{peer_id}"
        version_order = int(edited_at.timestamp()) if edited_at else 0
        await self.queue.enqueue(
            "telegram.ingest_event",
            {
                "event_type": event_type,
                "canonical_peer_id": peer_id,
                "telegram_dialog_id": int(event.chat_id),
                "dialog_title": str(
                    getattr(chat, "title", None)
                    or " ".join(
                        item
                        for item in (
                            getattr(chat, "first_name", None),
                            getattr(chat, "last_name", None),
                        )
                        if item
                    )
                    or "Telegram dialog"
                ),
                "dialog_username": getattr(chat, "username", None),
                "source_type": source_type,
                "is_automated": bool(
                    source_type == "personal" and is_automated_private_entity(chat)
                ),
                "telegram_message_id": int(message.id),
                "sender_id": int(message.sender_id) if message.sender_id else None,
                "sender_username": None,
                "sent_at": message.date.isoformat(),
                "edited_at": edited_at.isoformat() if edited_at else None,
                "outgoing": bool(message.out),
                "text": message.message or None,
                "attachments": attachments,
                "ingestion_source": "live",
            },
            tenant_id=self.connection.tenant_id,
            telegram_account_id=self.connection.id,
            priority=JOB_PRIORITY["P1"],
            idempotency_key=(
                f"telegram-event:{self.connection.id}:{peer_id}:{message.id}:{event_type}:{event_version}"
            ),
            correlation_id=str(uuid4()),
            category="realtime",
            cost_class="light",
            max_attempts=5,
            scheduled_at=datetime.now(UTC) + timedelta(milliseconds=250),
            partition_key=partition_key,
            partition_sequence=int(message.id) * 1_000_000_000 + version_order,
        )
        await self._increment_update(edited=event_type == "edited")

    async def _is_monitored_source(self, peer_id: str) -> bool:
        cached = getattr(self, "_monitored_peer_ids", None)
        if cached is None:
            async with self.transactions.session_factory() as session:
                cached = set(
                    await session.scalars(
                        select(MonitoredSource.canonical_peer_id).where(
                            MonitoredSource.connection_id == self.connection.id,
                            MonitoredSource.enabled.is_(True),
                        )
                    )
                )
            self._monitored_peer_ids = cached
        return peer_id in cached

    async def catch_up(self) -> dict[str, int]:
        """Fetch only gaps after saved cursors; used after restart/reconnect and rarely by scheduler."""
        # StringSession does not persist Telethon's entity cache. Rebuild it once
        # per catch-up pass and use InputPeer objects instead of unresolved raw IDs.
        # The full iter_dialogs pass is also the recovery path for newly discovered
        # personal chats. Groups/channels remain explicit opt-in sources.
        now = datetime.now(UTC)
        cached_catalog = getattr(self, "_remote_catalog", None)
        refreshed_at = getattr(self, "_catalog_refreshed_at", None)
        catalog_is_fresh = bool(
            cached_catalog
            and refreshed_at is not None
            and now - refreshed_at < CATCH_UP_CATALOG_TTL
        )
        remote_catalog: dict[int, dict[str, Any]] = cached_catalog or {}
        catalog_rate_limited = False
        try:
            if not catalog_is_fresh:
                refreshed_catalog: dict[int, dict[str, Any]] = {}
                try:
                    async with self.rpc_lock:
                        async for remote_dialog in self.client.iter_dialogs():
                            entity = remote_dialog.entity
                            source_type = (
                                "personal"
                                if remote_dialog.is_user
                                else "channel"
                                if remote_dialog.is_channel and getattr(entity, "broadcast", False)
                                else "group"
                            )
                            remote_id = int(remote_dialog.id)
                            refreshed_catalog[remote_id] = {
                                "input_entity": getattr(
                                    remote_dialog, "input_entity", remote_dialog.entity
                                ),
                                "title": str(remote_dialog.name or "Telegram dialog")[:300],
                                "username": getattr(entity, "username", None),
                                "source_type": source_type,
                                "last_message_at": getattr(remote_dialog.message, "date", None),
                                "is_automated": bool(
                                    source_type == "personal"
                                    and is_automated_private_entity(entity)
                                ),
                            }
                except errors.FloodWaitError as exc:
                    await self._rate_limited(int(exc.seconds))
                    if not refreshed_catalog:
                        raise TelegramFloodWait(int(exc.seconds)) from None
                    catalog_rate_limited = True
                    log_event(
                        logger,
                        logging.WARNING,
                        "telegram_catalog_partially_refreshed",
                        tenant_id=self.connection.tenant_id,
                        account_id=self.connection.id,
                        stage="telegram.catch_up",
                        catalog_size=len(refreshed_catalog),
                        retry_after_seconds=int(exc.seconds),
                    )
                remote_catalog = refreshed_catalog
                self._remote_catalog = refreshed_catalog
                self._catalog_refreshed_at = now
        except errors.FloodWaitError as exc:
            await self._rate_limited(int(exc.seconds))
            raise TelegramFloodWait(int(exc.seconds)) from None

        async def discover_personal_dialogs(session: AsyncSession) -> int:
            existing = {
                item.telegram_dialog_id: item
                for item in await session.scalars(
                    select(TelegramDialog).where(TelegramDialog.connection_id == self.connection.id)
                )
            }
            discovered = 0
            for remote_id, remote in remote_catalog.items():
                current = existing.get(remote_id)
                if remote["source_type"] != "personal":
                    continue
                if remote["is_automated"]:
                    if current is not None:
                        current.classification = "automated_account"
                        current.selected = False
                        current.excluded = True
                    continue
                if current is not None:
                    continue
                session.add(
                    TelegramDialog(
                        tenant_id=self.connection.tenant_id,
                        connection_id=self.connection.id,
                        telegram_dialog_id=remote_id,
                        canonical_peer_id=str(remote_id),
                        title=remote["title"],
                        username=remote["username"],
                        dialog_type="personal",
                        source="recovery",
                        classification="auto_personal",
                        confidence=1.0,
                        requires_user_confirmation=False,
                        selected=True,
                        excluded=False,
                        last_message_at=remote["last_message_at"],
                    )
                )
                discovered += 1
            return discovered

        discovered = await self.transactions.run(discover_personal_dialogs)
        if catalog_rate_limited:
            return {"events": 0, "discovered_dialogs": discovered}
        async with self.transactions.session_factory() as session:
            rows = (
                await session.execute(
                    select(TelegramDialog, TelegramIncrementalCursor)
                    .outerjoin(
                        TelegramIncrementalCursor,
                        TelegramIncrementalCursor.dialog_id == TelegramDialog.id,
                    )
                    .where(
                        TelegramDialog.connection_id == self.connection.id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            ).all()
        seen = 0
        for dialog, cursor in rows:
            remote = remote_catalog.get(dialog.telegram_dialog_id)
            input_entity = remote["input_entity"] if remote else None
            if input_entity is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "telegram_catchup_dialog_unavailable",
                    tenant_id=self.connection.tenant_id,
                    account_id=self.connection.id,
                    dialog_id=dialog.id,
                )
                continue
            page_after_id = cursor.last_message_id if cursor else dialog.last_message_id
            while True:
                page: list[Any] = []
                try:
                    async with self.rpc_lock:
                        async for message in self.client.iter_messages(
                            input_entity,
                            min_id=page_after_id,
                            reverse=True,
                            limit=CATCH_UP_PAGE_SIZE,
                        ):
                            page.append(message)
                except errors.FloodWaitError as exc:
                    await self._rate_limited(int(exc.seconds))
                    raise TelegramFloodWait(int(exc.seconds)) from None
                if not page:
                    break
                for message in page:
                    attachments: list[dict[str, Any]] = []
                    if message.media:
                        file = getattr(message, "file", None)
                        attachments.append(
                            {
                                "kind": type(message.media).__name__,
                                "name": getattr(file, "name", None),
                                "size": getattr(file, "size", None),
                                "mime_type": getattr(file, "mime_type", None),
                            }
                        )
                    await self.queue.enqueue(
                        "telegram.ingest_event",
                        {
                            "event_type": "new",
                            "canonical_peer_id": dialog.canonical_peer_id,
                            "telegram_dialog_id": dialog.telegram_dialog_id,
                            "dialog_title": dialog.title,
                            "dialog_username": dialog.username,
                            "source_type": dialog.dialog_type,
                            "telegram_message_id": int(message.id),
                            "sender_id": int(message.sender_id) if message.sender_id else None,
                            "sender_username": None,
                            "sent_at": message.date.isoformat(),
                            "edited_at": message.edit_date.isoformat()
                            if message.edit_date
                            else None,
                            "outgoing": bool(message.out),
                            "text": message.message or None,
                            "attachments": attachments,
                            "ingestion_source": "catchup",
                        },
                        tenant_id=self.connection.tenant_id,
                        telegram_account_id=self.connection.id,
                        dialog_id=dialog.id,
                        priority=JOB_PRIORITY["P1"],
                        idempotency_key=(
                            f"telegram-event:{self.connection.id}:{dialog.canonical_peer_id}:"
                            f"{message.id}:new:new"
                        ),
                        correlation_id=str(uuid4()),
                        category="realtime",
                        cost_class="light",
                        max_attempts=5,
                        partition_key=(
                            f"telegram-dialog:{self.connection.tenant_id}:"
                            f"{dialog.canonical_peer_id}"
                        ),
                        partition_sequence=int(message.id) * 1_000_000_000,
                    )
                    seen += 1
                page_after_id = max(int(message.id) for message in page)
                # The queue write above is durable. Checkpoint the highest
                # enqueued message now instead of waiting for the downstream
                # ingestion backlog to drain. A crash before this checkpoint
                # only causes idempotent re-enqueues; a successful checkpoint
                # prevents every 30-second reconciliation pass from walking the
                # same Telegram history and querying thousands of idempotency keys.
                checkpoint_message_id = page_after_id

                async def checkpoint(
                    session: AsyncSession,
                    dialog_id: str = dialog.id,
                    message_id: int = checkpoint_message_id,
                ) -> None:
                    current = await session.scalar(
                        select(TelegramIncrementalCursor).where(
                            TelegramIncrementalCursor.connection_id == self.connection.id,
                            TelegramIncrementalCursor.dialog_id == dialog_id,
                        )
                    )
                    now = datetime.now(UTC)
                    if current is None:
                        session.add(
                            TelegramIncrementalCursor(
                                tenant_id=self.connection.tenant_id,
                                connection_id=self.connection.id,
                                dialog_id=dialog_id,
                                last_message_id=message_id,
                                last_sync_at=now,
                                status="queued",
                            )
                        )
                    else:
                        current.last_message_id = max(current.last_message_id or 0, message_id)
                        current.last_sync_at = now
                        current.status = "queued"

                await self.transactions.run(checkpoint)
                if len(page) < CATCH_UP_PAGE_SIZE:
                    break

        if seen:

            async def write(session: AsyncSession) -> None:
                connection = await session.get(TelegramConnection, self.connection.id)
                if connection:
                    connection.catchup_events += seen

            await self.transactions.run(write)
        return {"events": seen, "discovered_dialogs": discovered}

    async def preview_source(self, link: str) -> dict[str, Any]:
        async with self.rpc_lock:
            normalized = link.strip().split("?", 1)[0].rstrip("/")
            if match := re.search(r"t\.me/(?:addlist/)([A-Za-z0-9_-]+)$", normalized):
                slug = match.group(1)
                invite = await self.client(functions.chatlists.CheckChatlistInviteRequest(slug))
                peers = []
                for chat in getattr(invite, "chats", []):
                    peers.append(
                        {
                            "canonical_peer_id": str(utils.get_peer_id(chat)),
                            "title": str(getattr(chat, "title", "Telegram group")),
                            "source_type": "channel"
                            if getattr(chat, "broadcast", False)
                            else "group",
                            "participants_count": getattr(chat, "participants_count", None),
                        }
                    )
                return {"kind": "folder", "token": slug, "peers": peers, "requires_join": True}
            if match := re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)$", normalized):
                invite_hash = match.group(1)
                invite = await self.client(functions.messages.CheckChatInviteRequest(invite_hash))
                chat = getattr(invite, "chat", None)
                return {
                    "kind": "group",
                    "token": invite_hash,
                    "peers": [
                        {
                            "canonical_peer_id": (
                                str(utils.get_peer_id(chat)) if chat is not None else "pending"
                            ),
                            "title": str(
                                getattr(chat, "title", getattr(invite, "title", "Telegram group"))
                            ),
                            "source_type": "group",
                            "participants_count": getattr(
                                chat,
                                "participants_count",
                                getattr(invite, "participants_count", None),
                            ),
                        }
                    ],
                    "requires_join": chat is None,
                }
            username = normalized.rsplit("/", 1)[-1].lstrip("@")
            entity = await self.client.get_entity(username)
            return {
                "kind": "group",
                "token": username,
                "peers": [
                    {
                        "canonical_peer_id": str(utils.get_peer_id(entity)),
                        "title": str(getattr(entity, "title", username)),
                        "source_type": "channel"
                        if getattr(entity, "broadcast", False)
                        else "group",
                        "participants_count": getattr(entity, "participants_count", None),
                    }
                ],
                "requires_join": False,
            }

    async def confirm_sources(
        self, preview: dict[str, Any], selected_peer_ids: list[str], join: bool
    ) -> dict[str, Any]:
        async with self.rpc_lock:
            if preview.get("requires_join") and not join:
                return {"requires_join": True, "added": 0}
            if join and preview.get("kind") == "group" and preview.get("requires_join"):
                updates = await self.client(
                    functions.messages.ImportChatInviteRequest(str(preview["token"]))
                )
                chats = list(getattr(updates, "chats", []))
                if not chats:
                    raise RuntimeError("Telegram did not return the joined group")
                chat = chats[0]
                preview = {
                    **preview,
                    "requires_join": False,
                    "peers": [
                        {
                            "canonical_peer_id": str(utils.get_peer_id(chat)),
                            "title": str(getattr(chat, "title", "Telegram group")),
                            "source_type": (
                                "channel" if getattr(chat, "broadcast", False) else "group"
                            ),
                            "participants_count": getattr(chat, "participants_count", None),
                        }
                    ],
                }
                selected_peer_ids = [str(utils.get_peer_id(chat))]
            elif join and preview.get("kind") == "folder":
                peers = [
                    await self.client.get_input_entity(int(peer_id))
                    for peer_id in selected_peer_ids
                ]
                await self.client(
                    functions.chatlists.JoinChatlistInviteRequest(
                        slug=str(preview["token"]), peers=peers
                    )
                )
            chosen = [
                item
                for item in preview.get("peers", [])
                if str(item.get("canonical_peer_id")) in set(selected_peer_ids)
            ]

            async def write(session: AsyncSession) -> int:
                added = 0
                for item in chosen:
                    peer_id = str(item["canonical_peer_id"])
                    source = await session.scalar(
                        select(MonitoredSource).where(
                            MonitoredSource.connection_id == self.connection.id,
                            MonitoredSource.canonical_peer_id == peer_id,
                        )
                    )
                    if source is None:
                        source = MonitoredSource(
                            tenant_id=self.connection.tenant_id,
                            connection_id=self.connection.id,
                            canonical_peer_id=peer_id,
                            source_type=str(item["source_type"]),
                            added_via="folder_link"
                            if preview.get("kind") == "folder"
                            else "group_link",
                            title=str(item["title"]),
                            metadata_json={"participants_count": item.get("participants_count")},
                        )
                        session.add(source)
                        added += 1
                    else:
                        source.enabled = True
                    dialog = await session.scalar(
                        select(TelegramDialog).where(
                            TelegramDialog.connection_id == self.connection.id,
                            TelegramDialog.canonical_peer_id == peer_id,
                        )
                    )
                    if dialog:
                        dialog.selected = True
                        dialog.excluded = False
                    else:
                        session.add(
                            TelegramDialog(
                                tenant_id=self.connection.tenant_id,
                                connection_id=self.connection.id,
                                telegram_dialog_id=int(peer_id),
                                canonical_peer_id=peer_id,
                                title=str(item["title"]),
                                dialog_type=str(item["source_type"]),
                                source="opt_in_link",
                                classification="opt_in",
                                selected=True,
                                excluded=False,
                                participants_count=item.get("participants_count"),
                            )
                        )
                return added

            added = await self.transactions.run(write)
            self._monitored_peer_ids = None
            return {"added": added, "requires_join": False}

    async def _increment_update(self, *, edited: bool = False) -> None:
        self._updates_received += 1
        if edited:
            self._edited_received += 1

    async def _owns_runtime(self) -> bool:
        return await self.leases.is_current(self.ownership)

    async def _flush_counters(self) -> None:
        updates, edited = self._updates_received, self._edited_received
        if not updates and not edited:
            return
        if not await self._owns_runtime():
            return
        self._updates_received = 0
        self._edited_received = 0

        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, self.connection.id)
            if connection:
                connection.updates_received += updates
                connection.edited_updates_received += edited
                connection.runtime_heartbeat_at = datetime.now(UTC)

        await self.transactions.run(write)

    async def _rate_limited(self, seconds: int) -> None:
        if not await self._owns_runtime():
            return
        until = datetime.now(UTC) + timedelta(seconds=max(1, seconds))

        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, self.connection.id)
            if connection:
                connection.runtime_status = "rate_limited"
                connection.health_status = "healthy"
                connection.rate_limited_until = until
                connection.last_error_code = "flood_wait"

        await self.transactions.run(write)

    async def _fatal_auth(self, error_code: str) -> None:
        if not await self._owns_runtime():
            return

        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, self.connection.id)
            if connection:
                connection.status = "reauthorization_required"
                connection.runtime_status = "reauthorization_required"
                connection.health_status = "revoked"
                connection.last_error_code = error_code

        await self.transactions.run(write)

    async def _health(self, status: str, *, reconnects: int, error: str | None = None) -> None:
        if not await self._owns_runtime():
            return

        async def write(session: AsyncSession) -> None:
            connection = await session.get(TelegramConnection, self.connection.id)
            if connection:
                connection.runtime_status = status
                connection.health_status = "healthy" if status == "running" else status
                connection.runtime_heartbeat_at = datetime.now(UTC)
                connection.reconnect_count = reconnects
                connection.last_reconnect_at = (
                    datetime.now(UTC) if status == "reconnecting" else connection.last_reconnect_at
                )
                connection.last_error_code = error

        await self.transactions.run(write)


class TelegramSessionRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        gateway: TelethonGateway,
        queue: SQLiteJobQueue,
        instance_id: str,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.gateway = gateway
        self.queue = queue
        self.instance_id = instance_id
        self.leases = TelegramRuntimeLeaseStore(session_factory)
        self.actors: dict[str, tuple[TelegramSessionActor, asyncio.Task[None]]] = {}

    async def reconcile(self) -> None:
        finished = [
            connection_id for connection_id, (_, task) in self.actors.items() if task.done()
        ]
        for connection_id in finished:
            _, task = self.actors.pop(connection_id)
            await asyncio.gather(task, return_exceptions=True)
        async with self.session_factory() as session:
            connections = list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.session_secret_id.is_not(None),
                        TelegramConnection.status.in_(("connected", "syncing", "ready")),
                    )
                )
            )
            desired = {item.id: item for item in connections}
            for connection_id, connection in desired.items():
                if connection_id in self.actors:
                    continue
                ownership = await self.leases.acquire(connection_id, self.instance_id)
                if ownership is None:
                    continue
                secret = await session.get(EncryptedSecret, connection.session_secret_id)
                if secret is None or secret.deleted_at is not None:
                    await self.leases.release(ownership)
                    continue
                session_string = self.encryption.decrypt(secret.ciphertext)
                actor = TelegramSessionActor(
                    connection,
                    session_string,
                    self.gateway,
                    self.queue,
                    self.session_factory,
                    ownership,
                    self.leases,
                    TelegramSyncHandlers(
                        self.session_factory,
                        self.encryption,
                        self.gateway,
                        batch_size=get_settings().telegram_sync_batch_size,
                        batch_pause_seconds=get_settings().telegram_sync_batch_pause_seconds,
                        max_messages_per_chat=get_settings().telegram_sync_max_messages_per_chat,
                    ),
                )
                self.actors[connection_id] = (
                    actor,
                    asyncio.create_task(actor.run(), name=f"telegram-session-{connection_id}"),
                )
            stale = set(self.actors) - set(desired)
        for connection_id in stale:
            actor, task = self.actors.pop(connection_id)
            await actor.stop()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        actors = list(self.actors.values())
        self.actors.clear()
        await asyncio.gather(*(actor.stop() for actor, _ in actors), return_exceptions=True)
        await asyncio.gather(*(task for _, task in actors), return_exceptions=True)

    async def run(self) -> None:
        try:
            while True:
                await self.reconcile()
                await asyncio.sleep(5)
        finally:
            await self.close()


async def run() -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    settings = await load_runtime_secret_overrides(session_factory, settings)
    configure_structured_logging(settings.log_level)
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("Telegram API credentials are required")
    runtime = TelegramSessionRuntime(
        session_factory,
        EncryptionService(settings.app_encryption_key.get_secret_value()),
        TelethonGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            device_model=settings.telegram_device_model,
            system_version=settings.telegram_system_version,
            app_version=settings.telegram_app_version,
            lang_code=settings.telegram_lang_code,
            system_lang_code=settings.telegram_system_lang_code,
        ),
        SQLiteJobQueue(session_factory),
        f"{socket.gethostname()}:{uuid4()}",
    )
    log_event(logger, logging.INFO, "telegram_session_runtime_started")
    await runtime.run()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
