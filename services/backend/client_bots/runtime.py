from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..bot.sqlite_storage import SQLiteFSMStorage
from ..database import SQLiteTransactionManager
from ..models import BotInstance, InitialAnalysisRun
from ..services.encryption import EncryptionService
from ..services.product_events import ProductEventService
from ..telegram_sessions.service import TelegramConnectionService
from .handlers import TenantOwnerMiddleware, build_client_router

logger = logging.getLogger(__name__)


class ClientBotRuntime(Protocol):
    async def run(self) -> None: ...

    async def stop(self) -> None: ...


class RuntimeFactory(Protocol):
    def __call__(self, token: str, tenant_id: str, bot_instance_id: str) -> ClientBotRuntime: ...


@dataclass(slots=True)
class RuntimeHandle:
    tenant_id: str
    secret_id: str
    generation: int
    runtime: ClientBotRuntime
    task: asyncio.Task[None]
    last_heartbeat: datetime


class AiogramPollingRuntime:
    def __init__(
        self,
        token: str,
        tenant_id: str,
        bot_instance_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        events: ProductEventService,
        *,
        mini_app_url: str | None,
        fsm_ttl: timedelta,
        connection_service: TelegramConnectionService | None = None,
    ) -> None:
        self.bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.session_factory = session_factory
        self.tenant_id = tenant_id
        self._progress_task: asyncio.Task[None] | None = None
        self.dispatcher = Dispatcher(storage=SQLiteFSMStorage(session_factory, ttl=fsm_ttl))
        router = build_client_router(
            events,
            mini_app_url=mini_app_url,
            connection_service=connection_service,
        )
        middleware = TenantOwnerMiddleware(
            session_factory,
            events,
            tenant_id=tenant_id,
            bot_instance_id=bot_instance_id,
        )
        router.message.middleware(middleware)
        router.callback_query.middleware(middleware)
        self.dispatcher.include_router(router)

    async def run(self) -> None:
        await self.bot.get_me()
        self._progress_task = asyncio.create_task(
            self._progress_loop(), name=f"telegram-progress:{self.tenant_id}"
        )
        try:
            await self.dispatcher.start_polling(self.bot, handle_signals=False)
        finally:
            if self._progress_task:
                self._progress_task.cancel()
                await asyncio.gather(self._progress_task, return_exceptions=True)

    async def _progress_loop(self) -> None:
        last_snapshot: tuple[object, ...] | None = None
        while True:
            await asyncio.sleep(2)
            async with self.session_factory() as session:
                run = await session.scalar(
                    select(InitialAnalysisRun)
                    .where(
                        InitialAnalysisRun.tenant_id == self.tenant_id,
                        InitialAnalysisRun.progress_chat_id.is_not(None),
                        InitialAnalysisRun.progress_message_id.is_not(None),
                    )
                    .order_by(InitialAnalysisRun.created_at.desc())
                    .limit(1)
                )
            if run is None:
                continue
            snapshot = (
                run.id,
                run.status,
                run.stage,
                run.progress_percent,
                run.completed_dialogs,
                run.failed_dialogs,
                run.messages_loaded,
                str(run.metrics_json),
            )
            if snapshot == last_snapshot:
                continue
            last_snapshot = snapshot
            filled = max(0, min(10, run.progress_percent // 10))
            bar = "●" * filled + "○" * (10 - filled)
            metrics = run.metrics_json or {}
            text_value = (
                "<b>Первичный анализ Telegram</b>\n\n"
                f"{bar} {run.progress_percent}%\n"
                f"Этап: {run.stage}\n"
                f"Диалоги: {run.completed_dialogs + run.failed_dialogs}/{run.total_dialogs}\n"
                f"Сообщения: {run.messages_loaded}\n"
                f"Ошибки отдельных чатов: {run.failed_dialogs}"
            )
            if run.status == "completed":
                text_value += (
                    "\n\n<b>Готово</b>\n"
                    f"Проблем найдено: {metrics.get('problems_created', 0)}\n"
                    f"Клиенты без ответа: {metrics.get('clients_without_answer', 0)}\n"
                    f"Жалобы: {metrics.get('complaints', 0)}"
                )
            rows = [[InlineKeyboardButton(text="↻ Обновить", callback_data="client:tg:progress")]]
            if run.status in {"pending", "running"} and not run.stop_requested:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="■ Остановить", callback_data=f"client:tg:stop:{run.id}"
                        )
                    ]
                )
            rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
            try:
                await self.bot.edit_message_text(
                    text=text_value,
                    chat_id=run.progress_chat_id,
                    message_id=run.progress_message_id,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.info(
                        "Could not update analysis progress for tenant %s (%s)",
                        self.tenant_id,
                        type(exc).__name__,
                    )

    async def stop(self) -> None:
        if self._progress_task:
            self._progress_task.cancel()
            await asyncio.gather(self._progress_task, return_exceptions=True)
            self._progress_task = None
        try:
            await self.dispatcher.stop_polling()
        except RuntimeError:
            pass
        await self.bot.session.close()


class ClientBotRuntimeManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        runtime_factory: RuntimeFactory,
        *,
        sync_interval_seconds: float = 2.0,
        heartbeat_seconds: float = 15.0,
        restart_backoff_seconds: float = 2.0,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.runtime_factory = runtime_factory
        self.sync_interval_seconds = sync_interval_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.restart_backoff_seconds = restart_backoff_seconds
        self.transactions = SQLiteTransactionManager(session_factory)
        self.events = ProductEventService(session_factory)
        self.handles: dict[str, RuntimeHandle] = {}
        self.retry_after: dict[str, datetime] = {}
        self._stopping = False

    async def sync_once(self) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            bots = list(
                await session.scalars(
                    select(BotInstance).where(
                        BotInstance.is_active.is_(True),
                        BotInstance.deleted_at.is_(None),
                    )
                )
            )
        by_id = {bot.id: bot for bot in bots}

        for bot_id, handle in list(self.handles.items()):
            bot = by_id.get(bot_id)
            restart_requested = bool(
                bot
                and bot.enabled
                and (
                    bot.runtime_generation != handle.generation or bot.secret_id != handle.secret_id
                )
            )
            if bot is None or not bot.enabled or restart_requested:
                await self._stop(bot_id, record_event=True)
                if restart_requested and bot is not None:
                    await self._start(bot)
                continue
            if handle.task.done():
                try:
                    await handle.runtime.stop()
                except Exception:  # noqa: BLE001 - cleanup must not stop other tenant runtimes
                    logger.warning("Client bot runtime %s cleanup failed", bot_id)
                if handle.task.cancelled():
                    await self._mark_status(bot, "stopped", None)
                else:
                    exc = handle.task.exception()
                    if exc is None:
                        await self._mark_status(bot, "stopped", None)
                    else:
                        await self._runtime_failed(bot, exc)
                        self.retry_after[bot_id] = now + timedelta(
                            seconds=self.restart_backoff_seconds
                        )
                self.handles.pop(bot_id, None)
                continue
            if (now - handle.last_heartbeat).total_seconds() >= self.heartbeat_seconds:
                await self._heartbeat(bot_id)
                handle.last_heartbeat = now

        for bot in bots:
            if not bot.enabled or bot.id in self.handles:
                continue
            retry_at = self.retry_after.get(bot.id)
            if retry_at and now < retry_at:
                continue
            await self._start(bot)

    async def _start(self, bot: BotInstance) -> None:
        await self._mark_status(bot, "starting", None)
        token: str | None = None
        try:
            token = self.encryption.decrypt(bot.secret.ciphertext)
            runtime = self.runtime_factory(token, bot.tenant_id, bot.id)
        except Exception as exc:  # noqa: BLE001 - isolates one tenant runtime from all others
            token = None
            await self._runtime_failed(bot, exc)
            self.retry_after[bot.id] = datetime.now(UTC) + timedelta(
                seconds=self.restart_backoff_seconds
            )
            return
        finally:
            token = None
        task = asyncio.create_task(runtime.run(), name=f"client-bot:{bot.id}")
        self.handles[bot.id] = RuntimeHandle(
            tenant_id=bot.tenant_id,
            secret_id=bot.secret_id,
            generation=bot.runtime_generation,
            runtime=runtime,
            task=task,
            last_heartbeat=datetime.now(UTC),
        )
        await self._mark_status(bot, "running", None, started=True)
        await self.events.record(
            tenant_id=bot.tenant_id,
            bot_instance_id=bot.id,
            event_name="client_bot_runtime_started",
        )
        await self.events.record(
            tenant_id=bot.tenant_id,
            bot_instance_id=bot.id,
            event_name="client_bot_started",
        )
        self.retry_after.pop(bot.id, None)

    async def _stop(self, bot_id: str, *, record_event: bool) -> None:
        handle = self.handles.pop(bot_id, None)
        if handle is None:
            return
        try:
            await handle.runtime.stop()
        finally:
            if not handle.task.done():
                handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
        async with self.session_factory() as session:
            bot = await session.get(BotInstance, bot_id)
        if bot is not None:
            await self._mark_status(bot, "stopped", None)
            if record_event:
                try:
                    await self.events.record(
                        tenant_id=bot.tenant_id,
                        bot_instance_id=bot.id,
                        event_name="client_bot_runtime_stopped",
                    )
                except LookupError:
                    logger.info("Runtime %s stopped after bot instance was soft-deleted", bot.id)

    async def _runtime_failed(self, bot: BotInstance, error: BaseException) -> None:
        safe_error = f"{type(error).__name__}: client bot runtime failed"
        logger.warning("Client bot runtime %s failed (%s)", bot.id, type(error).__name__)
        await self._mark_status(bot, "failed", safe_error, restart=True)
        await self.events.record(
            tenant_id=bot.tenant_id,
            bot_instance_id=bot.id,
            event_name="client_bot_runtime_failed",
            metadata={"error_type": type(error).__name__},
        )

    async def _mark_status(
        self,
        bot: BotInstance,
        status: str,
        error: str | None,
        *,
        started: bool = False,
        restart: bool = False,
    ) -> None:
        now = datetime.now(UTC)

        async def write(session: AsyncSession) -> None:
            values: dict[str, object] = {
                "runtime_status": status,
                "last_error": error,
                "runtime_heartbeat_at": now,
            }
            if started:
                values["last_started_at"] = now
            if restart:
                values["runtime_restart_count"] = BotInstance.runtime_restart_count + 1
            await session.execute(
                update(BotInstance)
                .where(BotInstance.id == bot.id, BotInstance.tenant_id == bot.tenant_id)
                .values(**values)
            )

        await self.transactions.run(write)

    async def _heartbeat(self, bot_id: str) -> None:
        async def write(session: AsyncSession) -> None:
            await session.execute(
                update(BotInstance)
                .where(BotInstance.id == bot_id, BotInstance.runtime_status == "running")
                .values(runtime_heartbeat_at=datetime.now(UTC))
            )

        await self.transactions.run(write)

    async def run(self) -> None:
        self._stopping = False
        while not self._stopping:
            await self.sync_once()
            await asyncio.sleep(self.sync_interval_seconds)

    async def stop(self) -> None:
        self._stopping = True
        for bot_id in list(self.handles):
            await self._stop(bot_id, record_event=True)
