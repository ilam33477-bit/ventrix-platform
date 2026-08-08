from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..bot.keyboards import (
    back_to_client_menu,
    client_main_menu,
    client_welcome_menu,
)
from ..models import (
    BotInstance,
    Employee,
    GroupIntegration,
    InitialAnalysisRun,
    OperationalProblem,
    Report,
    ReportMetric,
    Tenant,
    TenantAnalysisSchedule,
)
from ..services.product_events import ProductEventService
from ..telegram_sessions.service import TelegramConnectionError, TelegramConnectionService
from ..timezones import timezone_info
from .states import TelegramConnectionStates


@dataclass(frozen=True, slots=True)
class ClientContext:
    bot_instance_id: str
    tenant_id: str
    telegram_user_id: int
    role: str
    tenant: Tenant


class TenantOwnerMiddleware(BaseMiddleware):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        events: ProductEventService,
        *,
        tenant_id: str,
        bot_instance_id: str,
    ) -> None:
        self.session_factory = session_factory
        self.events = events
        self.tenant_id = tenant_id
        self.bot_instance_id = bot_instance_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = int(user.id) if user is not None else None
        await self.events.touch_update(
            tenant_id=self.tenant_id, bot_instance_id=self.bot_instance_id
        )
        async with self.session_factory() as session:
            tenant = await session.scalar(
                select(Tenant)
                .join(BotInstance, BotInstance.tenant_id == Tenant.id)
                .where(
                    Tenant.id == self.tenant_id,
                    Tenant.deleted_at.is_(None),
                    BotInstance.id == self.bot_instance_id,
                    BotInstance.tenant_id == self.tenant_id,
                    BotInstance.enabled.is_(True),
                    BotInstance.deleted_at.is_(None),
                )
            )
        if tenant is None or user_id is None or user_id != tenant.owner_telegram_user_id:
            await self.events.record(
                tenant_id=self.tenant_id,
                bot_instance_id=self.bot_instance_id,
                telegram_user_id=user_id,
                event_name="unauthorized_access_attempt",
                metadata={"reason": "owner_id_mismatch" if user_id else "missing_user_id"},
            )
            if isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому проекту.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("У вас нет доступа к этому проекту.")
            return None
        data["client_context"] = ClientContext(
            bot_instance_id=self.bot_instance_id,
            tenant_id=self.tenant_id,
            telegram_user_id=user_id,
            role="tenant_owner",
            tenant=tenant,
        )
        return await handler(event, data)


def _hours(value: dict[str, Any]) -> str:
    return str(value.get("description") or value)


def _access_status(tenant: Tenant) -> str:
    if tenant.status != "active":
        return "Доступ приостановлен. Данные сохранены, новые анализы не запускаются."
    if tenant.subscription_expires_at is None:
        return "Доступ активен без указанной даты окончания."
    days = (tenant.subscription_expires_at - datetime.now(UTC).date()).days
    if days < 0:
        return "Доступ приостановлен после окончания срока. Данные проекта сохранены."
    prefix = "Доступ заканчивается" if days <= 7 else "Доступ активен"
    return f"{prefix} до {tenant.subscription_expires_at:%d.%m.%Y}. Осталось {days} дн."


def build_client_router(
    events: ProductEventService,
    *,
    mini_app_url: str | None,
    connection_service: TelegramConnectionService | None = None,
) -> Router:
    router = Router(name="tenant-client-inline")

    async def record(context: ClientContext, name: str, **metadata: Any) -> None:
        await events.record(
            tenant_id=context.tenant_id,
            bot_instance_id=context.bot_instance_id,
            telegram_user_id=context.telegram_user_id,
            event_name=name,
            metadata=metadata,
        )

    async def render(
        query: CallbackQuery,
        text_value: str,
        *,
        main: bool = False,
    ) -> None:
        markup = client_main_menu(mini_app_url) if main else back_to_client_menu()
        if query.message:
            try:
                await query.message.edit_text(text_value, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        await query.answer()

    async def edit_screen(
        query: CallbackQuery,
        text_value: str,
        markup: InlineKeyboardMarkup,
    ) -> None:
        if query.message:
            try:
                await query.message.edit_text(text_value, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        await query.answer()

    async def edit_saved_screen(
        state: FSMContext,
        bot: Any,
        text_value: str,
        markup: InlineKeyboardMarkup,
    ) -> None:
        data = await state.get_data()
        await bot.edit_message_text(
            text=text_value,
            chat_id=int(data["screen_chat_id"]),
            message_id=int(data["screen_message_id"]),
            reply_markup=markup,
        )

    async def latest_run(tenant_id: str) -> InitialAnalysisRun | None:
        if connection_service is None:
            return None
        async with connection_service.session_factory() as session:
            return await session.scalar(
                select(InitialAnalysisRun)
                .where(InitialAnalysisRun.tenant_id == tenant_id)
                .order_by(InitialAnalysisRun.created_at.desc())
                .limit(1)
            )

    def connection_actions(status: str | None) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if status in {"connected", "ready", "syncing"}:
            rows.append(
                [InlineKeyboardButton(text="📂 Выбрать область", callback_data="client:tg:catalog")]
            )
            rows.append(
                [InlineKeyboardButton(text="📊 Прогресс", callback_data="client:tg:progress")]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Отключить аккаунт", callback_data="client:tg:disconnect"
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🔐 Подключить аккаунт", callback_data="client:tg:intro"
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def progress_view(run: InitialAnalysisRun) -> tuple[str, InlineKeyboardMarkup]:
        filled = max(0, min(10, run.progress_percent // 10))
        bar = "●" * filled + "○" * (10 - filled)
        stage_names = {
            "history_sync": "Загрузка истории небольшими пакетами",
            "stopping": "Останавливаем безопасно",
            "stopped": "Остановлено",
            "completed": "Первичный анализ завершён",
        }
        metrics = run.metrics_json or {}
        text_value = (
            f"<b>{escape(stage_names.get(run.stage, run.stage))}</b>\n\n"
            f"{bar} {run.progress_percent}%\n"
            f"Диалоги: {run.completed_dialogs + run.failed_dialogs}/{run.total_dialogs}\n"
            f"Сообщения: {run.messages_loaded}\n"
            f"Ошибки отдельных чатов: {run.failed_dialogs}"
        )
        if run.status == "completed":
            text_value += (
                "\n\n<b>Результат</b>\n"
                f"Аккаунт: {escape(str(metrics.get('connected_account', 'Telegram')))}\n"
                f"Рабочая папка: {escape(str(metrics.get('working_folder', '')))}\n"
                f"Рабочие группы: {metrics.get('working_groups', 0)}\n"
                f"Рабочие каналы: {metrics.get('working_channels', 0)}\n"
                f"Личные диалоги: {metrics.get('personal_dialogs', 0)}\n"
                "Вероятные деловые личные диалоги: "
                f"{metrics.get('probable_business_personal_dialogs', 0)}\n"
                f"Проблем найдено: {metrics.get('problems_created', 0)}\n"
                f"Клиенты без ответа: {metrics.get('clients_without_answer', 0)}\n"
                f"Жалобы: {metrics.get('complaints', 0)}\n"
                f"Обещания: {metrics.get('promises', 0)}\n"
                f"Потенциальные сделки: {metrics.get('potential_deals', 0)}\n"
                f"Просроченные обязательства: {metrics.get('overdue_commitments', 0)}\n"
                f"Созвоны под риском: {metrics.get('calls_at_risk', 0)}\n"
                f"Системные недоработки: {metrics.get('system_gaps', 0)}"
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
        if run.status == "completed":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👤 Проверить личные диалоги",
                        callback_data="client:tg:personal_review",
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        return text_value, InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(CommandStart())
    async def start(message: Message, client_context: ClientContext) -> None:
        await record(client_context, "client_user_started_bot")
        await record(client_context, "client_menu_opened")
        await message.answer(
            "<b>Рабочие коммуникации под контролем</b>\n\n"
            "Система помогает находить потерянные обращения, просроченные договорённости "
            "и риски в рабочих Telegram-коммуникациях.\n\n"
            "Для начала настройте проект и подключите рабочий аккаунт.",
            reply_markup=client_welcome_menu(mini_app_url),
        )

    @router.callback_query(F.data == "client:menu")
    async def menu(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "client_menu_opened")
        await render(
            query,
            f"<b>{escape(client_context.tenant.name)}</b>\n\nВыберите нужное действие.",
            main=True,
        )

    @router.callback_query(F.data == "client:summary")
    async def summary(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "summary_opened")
        tenant = client_context.tenant
        try:
            now = datetime.now(timezone_info(tenant.settings.timezone))
        except ValueError:
            now = datetime.now(UTC)
        next_report = datetime.combine(now.date(), tenant.settings.daily_report_time, now.tzinfo)
        if next_report <= now:
            next_report += timedelta(days=1)
        async with events.session_factory() as session:
            schedule = await session.scalar(
                select(TenantAnalysisSchedule).where(TenantAnalysisSchedule.tenant_id == tenant.id)
            )
            report = await session.scalar(
                select(Report)
                .where(Report.tenant_id == tenant.id, Report.status == "ready")
                .order_by(Report.ready_at.desc())
                .limit(1)
            )
            metrics = {}
            if report:
                metrics = dict(
                    (
                        await session.execute(
                            select(ReportMetric.metric_key, ReportMetric.numeric_value).where(
                                ReportMetric.tenant_id == tenant.id,
                                ReportMetric.report_id == report.id,
                            )
                        )
                    ).all()
                )
        await render(
            query,
            f"<b>Сводка · {escape(tenant.name)}</b>\n\n"
            f"Требуют внимания: {int(metrics.get('problems', 0))}\n"
            f"Высокий приоритет: {int(metrics.get('high', 0))}\n"
            f"Средний приоритет: {int(metrics.get('medium', 0))}\n"
            f"Сообщений в отчёте: {int(metrics.get('messages', 0))}\n"
            f"Последний отчёт: {report.ready_at.isoformat() if report and report.ready_at else 'ещё не готов'}\n"
            f"Следующий анализ: {schedule.next_analysis_at.isoformat() if schedule and schedule.next_analysis_at else next_report.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"<b>Доступ к системе</b>\n{escape(_access_status(tenant))}",
        )

    @router.callback_query(F.data == "client:important")
    async def important(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "problems_opened")
        async with events.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(OperationalProblem)
                    .where(
                        OperationalProblem.tenant_id == client_context.tenant_id,
                        OperationalProblem.status.in_(("open", "needs_confirmation")),
                    )
                    .order_by(OperationalProblem.occurred_at.desc())
                    .limit(10)
                )
            )
        lines = [
            f"{index}. {escape(item.explanation)} · {escape(item.priority)}"
            for index, item in enumerate(rows, start=1)
        ]
        await render(
            query,
            "<b>Важное</b>\n\n" + ("\n".join(lines) if lines else "Открытых проблем нет."),
        )

    @router.callback_query(F.data == "client:reports")
    async def reports(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "reports_opened")
        async with events.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(Report)
                    .where(Report.tenant_id == client_context.tenant_id)
                    .order_by(Report.period_end.desc())
                    .limit(10)
                )
            )
        lines = [
            f"{index}. {item.period_end:%d.%m.%Y} · {escape(item.status)} · {escape(item.summary)}"
            for index, item in enumerate(rows, start=1)
        ]
        await render(
            query,
            "<b>Отчёты</b>\n\n" + ("\n\n".join(lines) if lines else "История отчётов пока пуста."),
        )

    @router.callback_query(F.data == "client:connections")
    @router.callback_query(F.data == "client:connect")
    async def connections(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "telegram_connection_started")
        if connection_service is None:
            await render(
                query,
                "<b>Подключение Telegram</b>\n\nМодуль готов, но оператор ещё не указал "
                "TELEGRAM_API_ID и TELEGRAM_API_HASH. Обратитесь к администратору проекта.",
            )
            return
        connection = await connection_service.get(client_context.tenant_id)
        status = connection.status if connection else None
        details = (
            f"Аккаунт: {escape(connection.display_name or connection.phone_masked or 'подключён')}\n"
            f"Состояние: {escape(status or 'не подключён')}\n"
            f"Папка: {escape(connection.selected_folder_title or 'не выбрана')}\n"
            f"Период: {connection.history_days} дней"
            if connection
            else "Рабочий аккаунт ещё не подключён."
        )
        await edit_screen(
            query,
            "<b>Подключение Telegram</b>\n\n"
            f"{details}\n\nДо явного выбора папки и периода сообщения не анализируются.",
            connection_actions(status),
        )

    @router.callback_query(F.data == "client:tg:intro")
    async def connection_intro(query: CallbackQuery) -> None:
        await edit_screen(
            query,
            "<b>Безопасное подключение рабочего Telegram</b>\n\n"
            "1. В Telegram создайте или выберите рабочую папку с клиентами и командами.\n"
            "2. Мы запросим телефон, одноразовый код и, только если включена, 2FA.\n"
            "3. Код и пароль сразу удаляются из чата и не сохраняются. Сессия хранится "
            "только в зашифрованном виде.\n"
            "4. Личные диалоги не включаются без отдельного согласия.\n\n"
            "Подключение можно остановить, а сохранённые данные — удалить.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✓ Согласен, продолжить", callback_data="client:tg:consent"
                        )
                    ],
                    [InlineKeyboardButton(text="← Назад", callback_data="client:connections")],
                ]
            ),
        )

    @router.callback_query(F.data == "client:tg:consent")
    async def connection_consent(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None:
            await query.answer()
            return
        await state.set_state(TelegramConnectionStates.phone)
        await state.set_data(
            {
                "screen_chat_id": query.message.chat.id,
                "screen_message_id": query.message.message_id,
            }
        )
        await edit_screen(
            query,
            "<b>Шаг 1 из 5 · Телефон</b>\n\n"
            "Отправьте номер рабочего Telegram-аккаунта в международном формате, например "
            "+79990000000. Сообщение будет удалено после обработки.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                ]
            ),
        )

    @router.message(TelegramConnectionStates.phone)
    async def receive_phone(
        message: Message, state: FSMContext, client_context: ClientContext
    ) -> None:
        phone = (message.text or "").strip()
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if connection_service is None:
            await state.clear()
            return
        try:
            connection = await connection_service.begin_login(client_context.tenant_id, phone)
        except (ValueError, TelegramConnectionError):
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Не удалось принять номер</b>\n\nПроверьте международный формат и повторите.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001 - never expose provider details to chat
            await record(client_context, "telegram_login_failed", error_type=type(exc).__name__)
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Telegram временно не принял запрос</b>\n\n"
                "Подождите немного и отправьте номер ещё раз.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        await state.update_data(connection_id=connection.id)
        await state.set_state(TelegramConnectionStates.code)
        await record(client_context, "telegram_code_sent")
        await edit_saved_screen(
            state,
            message.bot,
            "<b>Шаг 2 из 5 · Код Telegram</b>\n\n"
            f"Код отправлен для {escape(connection.phone_masked or 'рабочего аккаунта')}. "
            "Отправьте его одним сообщением. Код будет немедленно удалён и не сохранится.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                ]
            ),
        )

    async def show_folders(state: FSMContext, bot: Any, tenant_id: str) -> None:
        if connection_service is None:
            return
        data = await state.get_data()
        connection_id = data.get("connection_id")
        await connection_service.refresh_catalog(tenant_id, connection_id)
        folders = await connection_service.list_folders(tenant_id, connection_id)
        await state.set_state(None)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"📁 {folder.title} · {folder.chat_count}",
                    callback_data=f"client:tg:folder:{folder.telegram_folder_id}",
                )
            ]
            for folder in folders[:30]
        ]
        if not rows:
            rows.append(
                [InlineKeyboardButton(text="↻ Проверить снова", callback_data="client:tg:catalog")]
            )
        rows.append([InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")])
        await edit_saved_screen(
            state,
            bot,
            "<b>Шаг 3 из 5 · Рабочая папка</b>\n\n"
            "Выберите папку Telegram, которую вы заранее собрали для рабочих чатов. "
            "Только диалоги из неё будут включены автоматически.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.message(TelegramConnectionStates.code)
    async def receive_code(
        message: Message, state: FSMContext, client_context: ClientContext
    ) -> None:
        code = (message.text or "").replace(" ", "")
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if connection_service is None:
            await state.clear()
            return
        try:
            data = await state.get_data()
            connection = await connection_service.complete_login(
                client_context.tenant_id,
                connection_id=data.get("connection_id"),
                code=code,
            )
        except Exception as exc:  # noqa: BLE001 - auth errors are intentionally sanitized
            await record(client_context, "telegram_code_rejected", error_type=type(exc).__name__)
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Код не принят</b>\n\nПроверьте код и отправьте новый одним сообщением.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        if connection.status == "awaiting_2fa":
            await record(client_context, "telegram_2fa_requested")
            await state.set_state(TelegramConnectionStates.password)
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Дополнительная защита · 2FA</b>\n\n"
                "Отправьте облачный пароль Telegram. Он будет немедленно удалён и не сохранится.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        await record(client_context, "telegram_connection_completed")
        await show_folders(state, message.bot, client_context.tenant_id)

    @router.message(TelegramConnectionStates.password)
    async def receive_password(
        message: Message, state: FSMContext, client_context: ClientContext
    ) -> None:
        password = message.text or ""
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if connection_service is None:
            await state.clear()
            return
        try:
            data = await state.get_data()
            await connection_service.complete_login(
                client_context.tenant_id,
                connection_id=data.get("connection_id"),
                password=password,
            )
        except Exception as exc:  # noqa: BLE001 - auth errors are intentionally sanitized
            await record(client_context, "telegram_2fa_rejected", error_type=type(exc).__name__)
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Пароль не принят</b>\n\nПроверьте пароль и отправьте его снова.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        password = ""
        await record(client_context, "telegram_connection_completed")
        await show_folders(state, message.bot, client_context.tenant_id)

    @router.callback_query(F.data == "client:tg:catalog")
    async def refresh_folders(
        query: CallbackQuery, state: FSMContext, client_context: ClientContext
    ) -> None:
        if query.message is None or connection_service is None:
            await query.answer()
            return
        await state.update_data(
            screen_chat_id=query.message.chat.id,
            screen_message_id=query.message.message_id,
        )
        try:
            await show_folders(state, query.bot, client_context.tenant_id)
        except Exception as exc:  # noqa: BLE001 - sanitized Telegram provider error
            await record(client_context, "telegram_catalog_failed", error_type=type(exc).__name__)
            await query.answer("Не удалось обновить папки. Повторите позже.", show_alert=True)
            return
        await query.answer()

    @router.callback_query(F.data.startswith("client:tg:folder:"))
    async def choose_folder(query: CallbackQuery, state: FSMContext) -> None:
        folder_id = int((query.data or "").rsplit(":", 1)[1])
        await state.update_data(folder_id=folder_id)
        await edit_screen(
            query,
            "<b>Шаг 4 из 5 · Личные диалоги</b>\n\n"
            "Разрешить системе отдельно проверить личные диалоги и предложить только те, "
            "которые похожи на рабочие? Они не войдут в анализ автоматически: при низкой "
            "уверенности потребуется ваше подтверждение.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Да, предложить", callback_data="client:tg:personal:yes"
                        ),
                        InlineKeyboardButton(text="Нет", callback_data="client:tg:personal:no"),
                    ],
                    [InlineKeyboardButton(text="← К папкам", callback_data="client:tg:catalog")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("client:tg:personal:"))
    async def choose_personal(query: CallbackQuery, state: FSMContext) -> None:
        consent = (query.data or "").endswith(":yes")
        await state.update_data(personal_consent=consent)
        await edit_screen(
            query,
            "<b>Шаг 5 из 5 · Глубина истории</b>\n\n"
            "Для первого запуска рекомендуется 7 дней. История загружается постепенно, "
            "небольшими пакетами с паузами.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="3 дня", callback_data="client:tg:days:3"),
                        InlineKeyboardButton(text="✓ 7 дней", callback_data="client:tg:days:7"),
                    ],
                    [
                        InlineKeyboardButton(text="14 дней", callback_data="client:tg:days:14"),
                        InlineKeyboardButton(text="30 дней", callback_data="client:tg:days:30"),
                    ],
                    [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("client:tg:days:"))
    async def choose_days(
        query: CallbackQuery, state: FSMContext, client_context: ClientContext
    ) -> None:
        if connection_service is None:
            await query.answer()
            return
        days = int((query.data or "").rsplit(":", 1)[1])
        data = await state.get_data()
        connection = await connection_service.select_scope(
            client_context.tenant_id,
            int(data["folder_id"]),
            personal_dialogs_consent=bool(data.get("personal_consent")),
            history_days=days,
            connection_id=data.get("connection_id"),
        )
        await record(
            client_context,
            "work_folder_selected",
            folder_count=len(connection.selected_folder_ids),
        )
        if connection.personal_dialogs_consent:
            await record(client_context, "personal_dialog_consent_granted")
        await record(client_context, "onboarding_completed")
        await edit_screen(
            query,
            "<b>Всё готово к первому анализу</b>\n\n"
            f"Папка: {escape(connection.selected_folder_title or '')}\n"
            f"Период: {connection.history_days} дней\n"
            f"Личные диалоги: {'только после подтверждения' if connection.personal_dialogs_consent else 'не проверять'}\n\n"
            "Загрузка продолжится в фоне и восстановится после перезапуска.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="▶ Начать анализ", callback_data="client:tg:start")],
                    [
                        InlineKeyboardButton(
                            text="← Изменить папку", callback_data="client:tg:catalog"
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data == "client:tg:start")
    async def start_sync(
        query: CallbackQuery, state: FSMContext, client_context: ClientContext
    ) -> None:
        if query.message is None or connection_service is None:
            await query.answer()
            return
        try:
            data = await state.get_data()
            run = await connection_service.start_initial_sync(
                client_context.tenant_id,
                progress_chat_id=query.message.chat.id,
                progress_message_id=query.message.message_id,
                connection_id=data.get("connection_id"),
            )
        except TelegramConnectionError:
            await query.answer("Не выбраны рабочие диалоги.", show_alert=True)
            return
        await state.clear()
        await record(client_context, "initial_sync_started", run_id=run.id)
        text_value, markup = progress_view(run)
        await edit_screen(query, text_value, markup)

    @router.callback_query(F.data == "client:tg:progress")
    async def sync_progress(query: CallbackQuery, client_context: ClientContext) -> None:
        run = await latest_run(client_context.tenant_id)
        if run is None:
            await query.answer("Анализ ещё не запускался.", show_alert=True)
            return
        text_value, markup = progress_view(run)
        await record(
            client_context,
            "initial_sync_completed" if run.status == "completed" else "initial_sync_progress",
            run_id=run.id,
            progress_percent=run.progress_percent,
        )
        await edit_screen(query, text_value, markup)

    @router.callback_query(F.data.startswith("client:tg:stop:"))
    async def stop_sync(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is None:
            await query.answer()
            return
        run_id = (query.data or "").rsplit(":", 1)[1]
        await connection_service.stop_run(client_context.tenant_id, run_id)
        run = await latest_run(client_context.tenant_id)
        text_value, markup = progress_view(run)
        await edit_screen(query, text_value, markup)

    @router.callback_query(F.data == "client:tg:cancel")
    async def cancel_connection(
        query: CallbackQuery, state: FSMContext, client_context: ClientContext
    ) -> None:
        if connection_service is not None:
            await connection_service.cancel_login(client_context.tenant_id)
        await state.clear()
        await edit_screen(
            query,
            "<b>Подключение отменено</b>\n\nВы можете вернуться к нему в любое время.",
            connection_actions(None),
        )

    @router.callback_query(F.data == "client:tg:disconnect")
    async def disconnect_confirm(query: CallbackQuery) -> None:
        await edit_screen(
            query,
            "<b>Отключить рабочий аккаунт?</b>\n\n"
            "Зашифрованная Telegram-сессия будет удалена. Уже загруженные результаты "
            "останутся до отдельной очистки.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отключить", callback_data="client:tg:disconnect_confirm"
                        )
                    ],
                    [InlineKeyboardButton(text="← Назад", callback_data="client:connections")],
                ]
            ),
        )

    @router.callback_query(F.data == "client:tg:disconnect_confirm")
    async def disconnect_account(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is not None:
            await connection_service.disconnect(client_context.tenant_id)
        await edit_screen(
            query,
            "<b>Аккаунт отключён</b>\n\nСессия удалена. Сохранённые результаты можно "
            "оставить или удалить безвозвратно.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Удалить загруженные данные",
                            callback_data="client:tg:clear_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="← Подключения", callback_data="client:connections"
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data == "client:tg:clear_confirm")
    async def clear_confirm(query: CallbackQuery) -> None:
        await edit_screen(
            query,
            "<b>Удалить все Telegram-данные проекта?</b>\n\n"
            "Будут безвозвратно удалены сообщения, диалоги, найденные проблемы, прогресс "
            "и ключи сессии. Это действие нельзя отменить.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Удалить безвозвратно", callback_data="client:tg:clear_execute"
                        )
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="client:connections")],
                ]
            ),
        )

    @router.callback_query(F.data == "client:tg:clear_execute")
    async def clear_execute(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is not None:
            await connection_service.clear_data(client_context.tenant_id)
        await edit_screen(
            query,
            "<b>Telegram-данные удалены</b>\n\nМожно подключить аккаунт заново.",
            connection_actions(None),
        )

    @router.callback_query(F.data == "client:tg:personal_review")
    async def personal_review(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is None:
            await query.answer()
            return
        candidates = await connection_service.list_personal_candidates(client_context.tenant_id)
        rows: list[list[InlineKeyboardButton]] = []
        lines = ["<b>Личные диалоги · требуется решение</b>", ""]
        for index, dialog in enumerate(candidates[:10], start=1):
            confidence = round(dialog.confidence * 100)
            marker = (
                "нужно подтверждение"
                if dialog.requires_user_confirmation
                else dialog.classification
            )
            lines.append(f"{index}. {escape(dialog.title)} · {confidence}% · {escape(marker)}")
            if dialog.requires_user_confirmation:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"✓ {index} рабочий",
                            callback_data=f"client:tg:include:{dialog.id}",
                        ),
                        InlineKeyboardButton(
                            text=f"✕ {index} личный", callback_data=f"client:tg:exclude:{dialog.id}"
                        ),
                    ]
                )
        if not candidates:
            lines.append("Кандидатов нет: личные диалоги не включались или уже исключены.")
        rows.append(
            [InlineKeyboardButton(text="← К результату", callback_data="client:tg:progress")]
        )
        await edit_screen(query, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith("client:tg:include:"))
    @router.callback_query(F.data.startswith("client:tg:exclude:"))
    async def personal_decision(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is None:
            await query.answer()
            return
        parts = (query.data or "").split(":")
        include = parts[2] == "include"
        await connection_service.confirm_personal_dialog(
            client_context.tenant_id, parts[3], include=include
        )
        await personal_review(query, client_context)

    @router.callback_query(F.data == "client:onboarding")
    async def onboarding(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "onboarding_started")
        await render(
            query,
            "<b>Настройка проекта</b>\n\n"
            "Компания и базовые рекомендации уже созданы. Подробный мастер бизнеса, "
            "рабочих часов, SLA и правил анализа будет добавлен следующим вертикальным шагом.",
        )

    @router.callback_query(F.data == "client:settings")
    async def settings(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "settings_opened")
        tenant = client_context.tenant
        await render(
            query,
            f"<b>Настройки проекта</b>\n\n"
            f"Компания: {escape(tenant.name)}\n"
            f"Ниша: {escape(tenant.niche)}\n"
            f"Рабочие часы: {escape(_hours(tenant.settings.working_hours))}\n"
            f"SLA ответа: {tenant.settings.response_sla_minutes} мин.\n"
            f"Время отчёта: {tenant.settings.daily_report_time:%H:%M}\n"
            f"Часовой пояс: {escape(tenant.settings.timezone)}\n\n"
            f"Порог отчёта: {tenant.settings.signal_report_threshold}/100\n"
            f"Порог проблемы: {tenant.settings.signal_problem_threshold}/100\n"
            f"Срочное уведомление: {tenant.settings.signal_immediate_threshold}/100\n"
            f"Уведомления сотрудников: {'включены' if tenant.settings.employee_notifications_enabled else 'выключены'}\n"
            f"Напоминания в группах: {'включены' if tenant.settings.group_reminders_enabled else 'выключены'}\n\n"
            f"<b>Доступ к системе</b>\n{escape(_access_status(tenant))}",
        )

    @router.callback_query(F.data == "client:employees")
    async def employees(query: CallbackQuery, client_context: ClientContext) -> None:
        async with events.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(Employee)
                    .where(Employee.tenant_id == client_context.tenant_id)
                    .order_by(Employee.display_name)
                )
            )
        lines = [
            f"{index}. {escape(item.display_name)} · {escape(item.role)} · "
            f"порог {item.criticality_threshold}"
            for index, item in enumerate(rows, start=1)
        ]
        await render(
            query,
            "<b>Сотрудники</b>\n\n"
            + ("\n".join(lines) if lines else "Сотрудники ещё не добавлены через Mini App/API."),
        )

    def groups_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✓ Проверить подключение", callback_data="client:groups:check"
                    )
                ],
                [InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")],
            ]
        )

    @router.callback_query(F.data == "client:groups")
    async def groups(query: CallbackQuery, client_context: ClientContext) -> None:
        async with events.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(GroupIntegration)
                    .where(GroupIntegration.tenant_id == client_context.tenant_id)
                    .order_by(GroupIntegration.title)
                )
            )
        lines = [
            f"{'✅' if item.status == 'active' else '⏳'} {escape(item.title)} · "
            f"{item.participants_count or 0} участников"
            for item in rows
        ]
        await edit_screen(
            query,
            "<b>Рабочие группы</b>\n\n"
            "1. Откройте нужную Telegram-группу.\n"
            "2. Добавьте этого Ventrix-бота.\n"
            "3. Разрешите читать сообщения и отправлять напоминания.\n"
            "4. Вернитесь сюда и нажмите «Проверить подключение».\n\n"
            + ("\n".join(lines) if lines else "Подключённых групп пока нет."),
            groups_markup(),
        )

    @router.callback_query(F.data == "client:groups:check")
    async def check_groups(query: CallbackQuery, client_context: ClientContext) -> None:
        async with events.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(GroupIntegration).where(
                        GroupIntegration.tenant_id == client_context.tenant_id
                    )
                )
            )
            for item in rows:
                try:
                    member = await query.bot.get_chat_member(item.telegram_chat_id, query.bot.id)
                    item.status = (
                        "active" if member.status in {"member", "administrator"} else "revoked"
                    )
                    item.participants_count = await query.bot.get_chat_member_count(
                        item.telegram_chat_id
                    )
                    item.last_verified_at = datetime.now(UTC)
                except TelegramBadRequest:
                    item.status = "revoked"
            await session.commit()
        await groups(query, client_context)

    @router.my_chat_member()
    async def group_membership(event: ChatMemberUpdated, client_context: ClientContext) -> None:
        if event.chat.type not in {"group", "supergroup"}:
            return
        active = event.new_chat_member.status in {"member", "administrator"}
        async with events.session_factory() as session:
            row = await session.scalar(
                select(GroupIntegration).where(
                    GroupIntegration.tenant_id == client_context.tenant_id,
                    GroupIntegration.telegram_chat_id == event.chat.id,
                )
            )
            if row is None:
                row = GroupIntegration(
                    tenant_id=client_context.tenant_id,
                    bot_instance_id=client_context.bot_instance_id,
                    telegram_chat_id=event.chat.id,
                    title=event.chat.title or "Рабочая группа",
                )
                session.add(row)
            row.status = "active" if active else "revoked"
            row.last_verified_at = datetime.now(UTC)
            await session.commit()

    @router.callback_query(F.data == "client:panel")
    async def panel(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "miniapp_button_clicked")
        await render(
            query,
            "<b>Панель проекта</b>\n\nMini App ещё не подключена. Кнопка станет активной "
            "после настройки HTTPS-адреса фронтенда.",
        )

    @router.callback_query(F.data == "client:how")
    async def how_it_works(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "button_clicked", button="how_it_works")
        await render(
            query,
            "<b>Как это работает</b>\n\n"
            "1. Вы подключаете рабочий Telegram-аккаунт.\n"
            "2. Явно выбираете анализируемые чаты.\n"
            "3. Система ищет обращения без ответа, просроченные обещания и другие риски.\n"
            "4. Краткие уведомления приходят в бот, подробности открываются в панели.",
        )

    return router
