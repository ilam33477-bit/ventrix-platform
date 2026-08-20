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
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    WebAppInfo,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.problems import ProblemStatus

from ..bot.keyboards import (
    back_to_client_menu,
    client_main_menu,
    client_welcome_menu,
)
from ..intelligence.problem_lifecycle import (
    ACTIVE_PROBLEM_STATUSES,
    ProblemLifecycleService,
    TransitionRequest,
)
from ..models import (
    AnalysisRun,
    BotInstance,
    Employee,
    GroupIntegration,
    InitialAnalysisRun,
    NotificationLog,
    OperationalProblem,
    ProductEvent,
    Report,
    ReportMetric,
    ReportSection,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    Tenant,
    TenantAnalysisSchedule,
    TenantMembership,
    TenantSettings,
)
from ..reporting.pdf import build_report_pdf
from ..services.employee_access import claim_employee_by_username
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
    employee_id: str | None = None


PROBLEM_TYPE_LABELS = {
    "client_without_answer": "Клиент ждёт ответа",
    "customer_question": "Открытый вопрос клиента",
    "customer_complaint": "Жалоба клиента",
    "technical_problem": "Техническая проблема",
    "payment_question": "Вопрос об оплате",
    "commercial_opportunity": "Коммерческая возможность",
    "overdue_commitment": "Просроченное обещание",
    "commitment_risk": "Риск по обещанию",
}
PROBLEM_STATUS_LABELS = {
    "new": "Новая",
    "needs_confirmation": "Нужно проверить",
    "acknowledged": "Принята",
    "assigned": "Назначена",
    "in_progress": "В работе",
    "waiting": "Ожидает",
    "resolved": "Решена",
    "auto_resolved": "Решена автоматически",
    "reopened": "Открыта повторно",
    "false_positive": "Не проблема",
}
CONNECTION_STATUS_LABELS = {
    "awaiting_code": "ожидает код",
    "awaiting_2fa": "ожидает пароль 2FA",
    "connected": "подключён",
    "syncing": "идёт синхронизация",
    "ready": "готов",
    "reauthorization_required": "нужно подключить заново",
}


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
            membership = None
            if tenant is not None and user_id is not None:
                membership = await session.scalar(
                    select(TenantMembership).where(
                        TenantMembership.tenant_id == tenant.id,
                        TenantMembership.telegram_user_id == user_id,
                        TenantMembership.status == "active",
                    )
                )
                if membership is None:
                    membership = await claim_employee_by_username(
                        session,
                        tenant_id=tenant.id,
                        telegram_user_id=user_id,
                        telegram_username=getattr(user, "username", None),
                    )
                    if membership is not None:
                        await session.commit()
        if tenant is None or user_id is None or membership is None:
            await self.events.record(
                tenant_id=self.tenant_id,
                bot_instance_id=self.bot_instance_id,
                telegram_user_id=user_id,
                event_name="unauthorized_access_attempt",
                metadata={"reason": "membership_missing" if user_id else "missing_user_id"},
            )
            if isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому проекту.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("У вас нет доступа к этому проекту.")
            return None
        if (
            membership.role not in {"owner", "manager"}
            and isinstance(event, CallbackQuery)
            and not (event.data or "").startswith("np:")
        ):
            await event.answer("Для этого действия недостаточно прав.", show_alert=True)
            return None
        data["client_context"] = ClientContext(
            bot_instance_id=self.bot_instance_id,
            tenant_id=self.tenant_id,
            telegram_user_id=user_id,
            role=membership.role,
            tenant=tenant,
            employee_id=membership.employee_id,
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

    def problem_system_markup(problem_id: str) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if mini_app_url:
            separator = "&" if "?" in mini_app_url else "?"
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Посмотреть в системе",
                        web_app=WebAppInfo(
                            url=(
                                f"{mini_app_url}{separator}section=problems&problem_id={problem_id}"
                            )
                        ),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def problem_status_markup(
        problem_id: str, *, false_positive: bool = False
    ) -> InlineKeyboardMarkup:
        if false_positive:
            rows = [
                [
                    InlineKeyboardButton(
                        text="Вернуть как проблему", callback_data=f"np:restore:{problem_id}"
                    )
                ],
                *problem_system_markup(problem_id).inline_keyboard,
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(text="Решено", callback_data=f"np:close:{problem_id}"),
                    InlineKeyboardButton(
                        text="Не проблема", callback_data=f"np:false:{problem_id}"
                    ),
                ],
                *problem_system_markup(problem_id).inline_keyboard,
            ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def mark_problem_card(
        query: CallbackQuery,
        problem_id: str,
        *,
        status_text: str,
        note: str,
        false_positive: bool = False,
        restored: bool = False,
    ) -> None:
        if not query.message:
            return
        original = query.message.html_text or escape(query.message.text or "")
        marker = "\n\n<b>Статус ситуации</b>\n"
        if marker in original:
            original = original.split(marker, 1)[0]
        updated = (
            f"{original}{marker}<blockquote>{escape(status_text)}\n{escape(note)}</blockquote>"
        )
        try:
            await query.message.edit_text(
                updated,
                reply_markup=(
                    problem_status_markup(problem_id, false_positive=True)
                    if false_positive
                    else problem_status_markup(problem_id)
                    if restored
                    else problem_system_markup(problem_id)
                ),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

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

    async def key_metrics(tenant_id: str) -> dict[str, int]:
        async with events.session_factory() as session:
            problems = int(
                await session.scalar(
                    select(func.count(OperationalProblem.id)).where(
                        OperationalProblem.tenant_id == tenant_id,
                        OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                    )
                )
                or 0
            )
            waiting = int(
                await session.scalar(
                    select(func.count(OperationalProblem.id)).where(
                        OperationalProblem.tenant_id == tenant_id,
                        OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                        OperationalProblem.problem_type == "client_without_answer",
                    )
                )
                or 0
            )
            reports = int(
                await session.scalar(
                    select(func.count(Report.id)).where(
                        Report.tenant_id == tenant_id,
                        Report.summary != "Обработано сообщений: 0. Проблем: 0.",
                    )
                )
                or 0
            )
            employees = int(
                await session.scalar(
                    select(func.count(Employee.id)).where(
                        Employee.tenant_id == tenant_id, Employee.status == "active"
                    )
                )
                or 0
            )
        return {
            "problems": problems,
            "waiting": waiting,
            "reports": reports,
            "employees": employees,
        }

    def settings_markup() -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if mini_app_url:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⚙️ Настроить в Ventrix AI", web_app=WebAppInfo(url=mini_app_url)
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def connection_actions(status: str | None) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if status in {"connected", "ready", "syncing"}:
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
            account = escape(str(metrics.get("connected_account") or "Рабочий Telegram"))
            text_value += (
                "\n\n<b>✅ Первичная проверка завершена</b>\n"
                f"Аккаунт: {account}\n"
                f"Личных диалогов проверено: <b>{metrics.get('personal_dialogs', 0)}</b>\n"
                f"Рабочих групп: <b>{metrics.get('working_groups', 0)}</b>\n\n"
                "<b>Что нашёл анализ</b>\n"
                f"Подтверждённых ситуаций: <b>{metrics.get('problems_created', 0)}</b>\n"
                f"Обещаний сотрудников: <b>{metrics.get('promises', 0)}</b>\n"
                f"Жалоб: <b>{metrics.get('complaints', 0)}</b>\n"
                f"Потенциальных сделок: <b>{metrics.get('potential_deals', 0)}</b>\n\n"
                f"<i>{metrics.get('clients_without_answer', 0)} диалогов имели предварительный признак ожидания ответа. Они не считаются проблемами, пока контекст и последующие сообщения не подтвердят риск.</i>"
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
        tenant = client_context.tenant
        async with events.session_factory() as session:
            connections = list(
                await session.scalars(
                    select(TelegramConnection)
                    .where(
                        TelegramConnection.tenant_id == tenant.id,
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.status.in_(("connected", "syncing", "ready")),
                    )
                    .order_by(TelegramConnection.created_at.desc())
                )
            )
            previous_start = await session.scalar(
                select(ProductEvent)
                .where(
                    ProductEvent.tenant_id == tenant.id,
                    ProductEvent.telegram_user_id == client_context.telegram_user_id,
                    ProductEvent.event_name == "client_user_started_bot",
                )
                .order_by(ProductEvent.occurred_at.desc())
                .limit(1)
            )
            new_situations = 0
            if previous_start is not None:
                new_situations = int(
                    await session.scalar(
                        select(func.count(OperationalProblem.id)).where(
                            OperationalProblem.tenant_id == tenant.id,
                            OperationalProblem.created_at > previous_start.occurred_at,
                            OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                        )
                    )
                    or 0
                )
        await record(client_context, "client_user_started_bot")
        await record(client_context, "client_menu_opened")
        first_name = (
            (message.from_user.first_name if message.from_user else None)
            or (tenant.owner_name or "").strip().split()[0]
            or "Здравствуйте"
        )
        metrics = await key_metrics(tenant.id)
        if connections:
            activity = (
                f"За время отсутствия найдено новых ситуаций: <b>{new_situations}</b>."
                if new_situations
                else "Ничего критичного не обнаружено. Продолжаем мониторинг."
            )
            await message.answer(
                f"<b>{escape(first_name)}, добрый день.</b>\n\n"
                f"Ventrix продолжает следить за проектом <b>{escape(tenant.name)}</b>.\n\n"
                f"<blockquote>{activity}\n\n"
                f"Ситуации в работе: <b>{metrics['problems']}</b>\n"
                f"Клиенты ждут ответа: <b>{metrics['waiting']}</b>\n"
                f"Подключённые аккаунты: <b>{len(connections)}</b>\n"
                f"Готовые сводки: <b>{metrics['reports']}</b></blockquote>",
                reply_markup=client_main_menu(mini_app_url),
            )
            return
        await message.answer(
            f"<b>{escape(first_name)}, привет.</b>\n\n"
            f"Ventrix настроен для команды <b>{escape(tenant.name)}</b>"
            f"{f' в направлении «{escape(tenant.niche)}»' if tenant.niche else ''}.\n\n"
            "Он поможет замечать клиентов без ответа, незавершённые договорённости "
            "и ситуации, которым нужна реакция руководителя.\n\n"
            "Откройте Mini App, чтобы подключить рабочий Telegram и выбрать расписание сводок.",
            reply_markup=client_welcome_menu(mini_app_url),
        )

    @router.callback_query(F.data == "client:menu")
    async def menu(query: CallbackQuery, client_context: ClientContext) -> None:
        await record(client_context, "client_menu_opened")
        metrics = await key_metrics(client_context.tenant_id)
        await render(
            query,
            f"<b>{escape(client_context.tenant.name)}</b>\n\n"
            f"В работе: <b>{metrics['problems']}</b> · ждут ответа: <b>{metrics['waiting']}</b>\n"
            f"Последние действия доступны в панели Ventrix AI.\n\nВыберите действие:",
            main=True,
        )

    @router.callback_query(F.data.startswith("np:open:"))
    async def notification_problem_open(
        query: CallbackQuery, client_context: ClientContext
    ) -> None:
        problem_id = query.data.rsplit(":", 1)[1]
        async with events.session_factory() as session:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == client_context.tenant_id,
                )
            )
        if problem is None:
            await query.answer("Ситуация не найдена", show_alert=True)
            return
        if (
            client_context.role == "employee"
            and problem.responsible_employee_id != client_context.employee_id
        ):
            await query.answer("Эта ситуация назначена другому сотруднику", show_alert=True)
            return
        await edit_screen(
            query,
            f"<b>{escape(PROBLEM_TYPE_LABELS.get(problem.problem_type, 'Рабочая ситуация'))}</b>\n\n"
            f"Статус: <b>{escape(PROBLEM_STATUS_LABELS.get(problem.status, 'Требует проверки'))}</b>\n"
            f"Причина: {escape(problem.explanation)}\n\n"
            f"<blockquote>{escape(problem.evidence or 'Evidence появится после проверки')}</blockquote>\n\n"
            f"Следующий шаг: {escape(problem.recommended_action)}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Закрыть", callback_data=f"np:close:{problem.id}")],
                    *problem_system_markup(problem.id).inline_keyboard,
                ]
            ),
        )

    @router.callback_query(F.data.startswith("np:false:"))
    async def notification_false_positive(
        query: CallbackQuery, client_context: ClientContext
    ) -> None:
        if client_context.role not in {"owner", "manager"}:
            await query.answer("Действие доступно владельцу или менеджеру", show_alert=True)
            return
        problem_id = query.data.rsplit(":", 1)[1]
        lifecycle = ProblemLifecycleService(events.session_factory)
        try:
            problem = await lifecycle.transition(
                client_context.tenant_id,
                problem_id,
                TransitionRequest(
                    target=ProblemStatus.FALSE_POSITIVE,
                    actor_type="tenant_owner",
                    actor_id=str(client_context.telegram_user_id),
                    reason="Владелец отметил уведомление как ложное срабатывание.",
                ),
            )
        except ValueError:
            async with events.session_factory() as session:
                problem = await session.scalar(
                    select(OperationalProblem).where(
                        OperationalProblem.id == problem_id,
                        OperationalProblem.tenant_id == client_context.tenant_id,
                    )
                )
            if problem is None or problem.status != ProblemStatus.FALSE_POSITIVE.value:
                await query.answer("Текущий статус уже нельзя отметить как ложный", show_alert=True)
                return
        await mark_problem_card(
            query,
            problem_id,
            status_text="Не проблема",
            note="Карточка исключена из активных ситуаций и синхронизирована с Mini App.",
            false_positive=True,
        )
        await query.answer("Отмечено как не проблема", show_alert=True)
        await record(
            client_context,
            "problem_false_positive",
            problem_id=problem_id,
        )

    @router.callback_query(F.data.startswith("np:restore:"))
    async def notification_restore_problem(
        query: CallbackQuery, client_context: ClientContext
    ) -> None:
        if client_context.role not in {"owner", "manager"}:
            await query.answer("Действие доступно владельцу или менеджеру", show_alert=True)
            return
        problem_id = query.data.rsplit(":", 1)[1]
        try:
            await ProblemLifecycleService(events.session_factory).transition(
                client_context.tenant_id,
                problem_id,
                TransitionRequest(
                    target=ProblemStatus.REOPENED,
                    actor_type="tenant_owner",
                    actor_id=str(client_context.telegram_user_id),
                    reason="Владелец вернул ошибочно исключённую карточку в работу.",
                ),
            )
        except ValueError:
            await query.answer("Карточку уже нельзя вернуть этим действием", show_alert=True)
            return
        await mark_problem_card(
            query,
            problem_id,
            status_text="Снова в работе",
            note="Карточка возвращена в активные ситуации и Mini App.",
            restored=True,
        )
        await query.answer("Карточка возвращена", show_alert=True)
        await record(client_context, "problem_restored", problem_id=problem_id)

    @router.callback_query(F.data.startswith("np:close:"))
    async def notification_close(query: CallbackQuery, client_context: ClientContext) -> None:
        problem_id = query.data.rsplit(":", 1)[1]
        lifecycle = ProblemLifecycleService(events.session_factory)
        async with events.session_factory() as session:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == client_context.tenant_id,
                )
            )
        if problem is None:
            await query.answer("Ситуация не найдена", show_alert=True)
            return
        if (
            client_context.role == "employee"
            and problem.responsible_employee_id != client_context.employee_id
        ):
            await query.answer("Эта ситуация назначена другому сотруднику", show_alert=True)
            return
        try:
            if problem.status == ProblemStatus.NEEDS_CONFIRMATION.value:
                problem = await lifecycle.transition(
                    client_context.tenant_id,
                    problem.id,
                    TransitionRequest(
                        ProblemStatus.ACKNOWLEDGED,
                        "tenant_owner",
                        str(client_context.telegram_user_id),
                        "Ситуация подтверждена владельцем.",
                    ),
                )
            if (
                problem.status == ProblemStatus.ACKNOWLEDGED.value
                and problem.responsible_employee_id
            ):
                problem = await lifecycle.transition(
                    client_context.tenant_id,
                    problem.id,
                    TransitionRequest(
                        ProblemStatus.ASSIGNED,
                        "tenant_owner",
                        str(client_context.telegram_user_id),
                        "Ответственный подтверждён владельцем.",
                        responsible_employee_id=problem.responsible_employee_id,
                    ),
                )
            if problem.status == ProblemStatus.ASSIGNED.value:
                problem = await lifecycle.transition(
                    client_context.tenant_id,
                    problem.id,
                    TransitionRequest(
                        ProblemStatus.IN_PROGRESS,
                        "tenant_owner",
                        str(client_context.telegram_user_id),
                        "Владелец подтвердил выполнение действия.",
                    ),
                )
            if problem.status == ProblemStatus.REOPENED.value:
                problem = await lifecycle.transition(
                    client_context.tenant_id,
                    problem.id,
                    TransitionRequest(
                        ProblemStatus.IN_PROGRESS,
                        "tenant_owner",
                        str(client_context.telegram_user_id),
                        "Возвращённая проблема взята в работу перед закрытием.",
                    ),
                )
            if problem.status not in {ProblemStatus.IN_PROGRESS.value, ProblemStatus.WAITING.value}:
                await query.answer(
                    "Сначала подтвердите и назначьте ответственного в карточке", show_alert=True
                )
                return
            if problem.status == ProblemStatus.WAITING.value:
                problem = await lifecycle.transition(
                    client_context.tenant_id,
                    problem.id,
                    TransitionRequest(
                        ProblemStatus.IN_PROGRESS,
                        "tenant_owner",
                        str(client_context.telegram_user_id),
                        "Получено подтверждение владельца.",
                    ),
                )
            problem = await lifecycle.transition(
                client_context.tenant_id,
                problem.id,
                TransitionRequest(
                    ProblemStatus.RESOLVED,
                    "tenant_owner",
                    str(client_context.telegram_user_id),
                    "Закрыто владельцем из Telegram-уведомления.",
                ),
            )
        except ValueError:
            await query.answer("Переход статуса недоступен", show_alert=True)
            return
        await mark_problem_card(
            query,
            problem_id,
            status_text="Решено",
            note="Ситуация закрыта и обновлена в Mini App.",
        )
        await query.answer("Ситуация закрыта", show_alert=True)

    @router.callback_query(F.data.startswith("np:notify:"))
    async def notification_employee(query: CallbackQuery, client_context: ClientContext) -> None:
        if client_context.role not in {"owner", "manager"}:
            await query.answer("Действие доступно владельцу или менеджеру", show_alert=True)
            return
        problem_id = query.data.rsplit(":", 1)[1]
        async with events.session_factory() as session:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == client_context.tenant_id,
                )
            )
            employee = (
                await session.get(Employee, problem.responsible_employee_id)
                if problem and problem.responsible_employee_id
                else None
            )
            source = (
                await session.get(TelegramMessage, problem.source_message_id)
                if problem and problem.source_message_id
                else None
            )
            dialog = (
                await session.get(TelegramDialog, problem.dialog_id)
                if problem and problem.dialog_id
                else None
            )
            dedup_key = (
                f"manual-employee:{problem.id}:{problem.status}:{employee.id}"
                if problem and employee
                else ""
            )
            existing = (
                await session.scalar(
                    select(NotificationLog).where(NotificationLog.deduplication_key == dedup_key)
                )
                if dedup_key
                else None
            )
        if not employee or not employee.telegram_user_id:
            await query.answer("Ответственный сотрудник с Telegram ID не назначен", show_alert=True)
            return
        if existing is not None and existing.status in {"pending", "sent", "delivery_uncertain"}:
            await query.answer("Сотрудник уже уведомлён по этой версии ситуации", show_alert=True)
            return
        async with events.session_factory() as session:
            log = await session.get(NotificationLog, existing.id) if existing else None
            if log is None:
                log = NotificationLog(
                    tenant_id=client_context.tenant_id,
                    problem_id=problem.id,
                    employee_id=employee.id,
                    destination_type="employee",
                    destination_id=str(employee.telegram_user_id),
                    deduplication_key=dedup_key,
                    criticality=0,
                    payload_json={"manual": True, "problem_status": problem.status},
                )
                session.add(log)
            else:
                log.status = "pending"
                log.last_error_code = None
            await session.commit()
            log_id = log.id
        person = dialog.title if dialog else "клиентом"
        try:
            await query.bot.send_message(
                employee.telegram_user_id,
                f"⚠️ <b>В переписке с {escape(person)} требуется ваше внимание</b>\n\n"
                f"{escape(problem.explanation)}\n\n"
                f"<blockquote>{escape((source.body_text if source else None) or problem.evidence or 'Откройте карточку Ventrix')}</blockquote>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Открыть карточку", callback_data=f"np:open:{problem.id}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="Отметить выполненным",
                                callback_data=f"np:close:{problem.id}",
                            )
                        ],
                    ]
                ),
            )
        except Exception as exc:
            async with events.session_factory() as session:
                stored_log = await session.get(NotificationLog, log_id)
                stored_log.status = "failed"
                stored_log.last_error_code = type(exc).__name__
                await session.commit()
            raise
        async with events.session_factory() as session:
            stored_log = await session.get(NotificationLog, log_id)
            stored_log.status = "sent"
            stored_log.sent_at = datetime.now(UTC)
            await session.commit()
        await query.answer("Сотрудник уведомлён", show_alert=True)

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
        live = await key_metrics(tenant.id)
        last_report = (
            report.ready_at.strftime("%d.%m.%Y %H:%M")
            if report and report.ready_at
            else "после появления новых сообщений"
        )
        next_at = (
            schedule.next_analysis_at.strftime("%d.%m.%Y %H:%M")
            if schedule and schedule.next_analysis_at
            else next_report.strftime("%d.%m.%Y %H:%M")
        )
        await render(
            query,
            f"<b>📊 Ключевые показатели · {escape(tenant.name)}</b>\n\n"
            f"<blockquote><b>Сейчас требуют реакции</b>\nРабочие ситуации: <b>{live['problems']}</b>\nКлиенты ждут ответа: <b>{live['waiting']}</b></blockquote>\n\n"
            f"<blockquote><b>Последняя рабочая сводка</b>\nИзучено сообщений: <b>{int(metrics.get('messages', 0))}</b>\nВажных ситуаций: <b>{int(metrics.get('high', 0))}</b>\nСреднего приоритета: <b>{int(metrics.get('medium', 0))}</b>\n\nПоследняя сводка: {last_report}\nСледующая проверка: {next_at}</blockquote>",
            main=True,
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
            report_rows = list(
                await session.scalars(
                    select(Report)
                    .where(Report.tenant_id == client_context.tenant_id)
                    .order_by(Report.period_end.desc())
                    .limit(10)
                )
            )
        canonical: list[Report] = []
        seen: set[tuple[object, str]] = set()
        for item in report_rows:
            key = (item.period_end.date(), item.summary)
            if key in seen or item.summary == "Обработано сообщений: 0. Проблем: 0.":
                continue
            seen.add(key)
            canonical.append(item)
        lines = [
            f"• <b>{item.period_end:%d.%m.%Y}</b> — {escape(item.summary.replace('Обработано сообщений', 'изучено сообщений').replace('Проблем', 'ситуаций'))}"
            for item in canonical[:5]
        ]
        buttons = [
            [
                InlineKeyboardButton(
                    text="Сводка за 7 дней", callback_data="client:report:request:week"
                ),
                InlineKeyboardButton(
                    text="За 30 дней", callback_data="client:report:request:month"
                ),
            ]
        ]
        if mini_app_url:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Открыть архив в Ventrix AI", web_app=WebAppInfo(url=mini_app_url)
                    )
                ]
            )
        if canonical:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Скачать последний PDF",
                        callback_data=f"client:report:pdf:{canonical[0].id}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        await edit_screen(
            query,
            "<b>📄 Рабочие сводки</b>\n\n"
            + (
                "\n".join(lines)
                if lines
                else "Готовых сводок пока нет — пустые отчёты не создаются."
            )
            + "\n\n<i>Недельную сводку можно обновлять раз в день, месячную — раз в неделю.</i>",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data.startswith("client:report:request:"))
    async def request_report(query: CallbackQuery, client_context: ClientContext) -> None:
        if connection_service is None:
            await query.answer("Анализ временно недоступен", show_alert=True)
            return
        period = query.data.rsplit(":", 1)[1]
        days, cooldown = (7, timedelta(days=1)) if period == "week" else (30, timedelta(days=7))
        trigger = f"manual_{period}"
        async with events.session_factory() as session:
            recent = await session.scalar(
                select(AnalysisRun)
                .where(
                    AnalysisRun.tenant_id == client_context.tenant_id,
                    AnalysisRun.trigger == trigger,
                    AnalysisRun.created_at >= datetime.now(UTC) - cooldown,
                )
                .order_by(AnalysisRun.created_at.desc())
                .limit(1)
            )
        if recent:
            await query.answer(
                "Такая сводка уже запрошена. Новые данные ещё не накопились.", show_alert=True
            )
            return
        await connection_service.queue.enqueue(
            "analysis.pipeline",
            {
                "history_window_days": days,
                "trigger": trigger,
                "report_due_at": datetime.now(UTC).isoformat(),
            },
            tenant_id=client_context.tenant_id,
            priority=45,
            idempotency_key=f"{trigger}:{client_context.tenant_id}:{datetime.now(UTC).date().isoformat()}",
            category="analysis",
            is_heavy=True,
        )
        await query.answer(
            "Сводка поставлена в очередь. Она появится в разделе «Отчёты».", show_alert=True
        )

    @router.callback_query(F.data.startswith("client:report:pdf:"))
    async def report_pdf(query: CallbackQuery, client_context: ClientContext) -> None:
        report_id = (query.data or "").rsplit(":", 1)[-1]
        async with events.session_factory() as session:
            report = await session.scalar(
                select(Report).where(
                    Report.id == report_id,
                    Report.tenant_id == client_context.tenant_id,
                    Report.status == "ready",
                )
            )
            if report is None:
                await query.answer("Сводка не найдена", show_alert=True)
                return
            metrics = dict(
                (
                    await session.execute(
                        select(ReportMetric.metric_key, ReportMetric.numeric_value).where(
                            ReportMetric.report_id == report.id,
                            ReportMetric.tenant_id == client_context.tenant_id,
                        )
                    )
                ).all()
            )
            sections = {
                section.section_key: section.data_json
                for section in await session.scalars(
                    select(ReportSection).where(
                        ReportSection.report_id == report.id,
                        ReportSection.tenant_id == client_context.tenant_id,
                    )
                )
            }
        pdf = build_report_pdf(
            tenant_name=client_context.tenant.name,
            period_start=report.period_start.strftime("%d.%m.%Y"),
            period_end=report.period_end.strftime("%d.%m.%Y"),
            metrics={key: float(value) for key, value in metrics.items()},
            sections=sections,
        )
        if query.message:
            await query.message.answer_document(
                BufferedInputFile(
                    pdf,
                    filename=f"ventrix-{report.period_end:%Y-%m-%d}.pdf",
                ),
                caption="Рабочая сводка Ventrix",
            )
        await query.answer()

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
        rows = [
            item
            for item in await connection_service.get_all(client_context.tenant_id)
            if item.status
            in {
                "awaiting_code",
                "awaiting_2fa",
                "connected",
                "syncing",
                "ready",
                "reauthorization_required",
            }
        ]
        if not rows:
            await edit_screen(
                query,
                "<b>Подключения Telegram</b>\n\nРабочие аккаунты ещё не подключены.",
                connection_actions(None),
            )
            return
        details = []
        buttons = []
        for index, item in enumerate(rows, start=1):
            label = item.display_name or item.phone_masked or f"Аккаунт {index}"
            details.append(
                f"<b>{index}. {escape(label)}</b>\n"
                f"Статус: {escape(CONNECTION_STATUS_LABELS.get(item.status, 'требует проверки'))}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{index}. {label[:28]}", callback_data=f"ctv:{item.id}"
                    )
                ]
            )
        buttons.append(
            [InlineKeyboardButton(text="➕ Подключить аккаунт", callback_data="client:tg:intro")]
        )
        buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")])
        await edit_screen(
            query,
            f"<b>Подключения Telegram · {len(rows)}</b>\n\n<blockquote>{'\n\n'.join(details)}</blockquote>\n\nВыберите аккаунт, чтобы проверить его статистику и состояние.",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data.startswith("ctv:"))
    async def connection_detail(query: CallbackQuery, client_context: ClientContext) -> None:
        connection_id = (query.data or "").split(":", 1)[1]
        connection = (
            await connection_service.get(client_context.tenant_id, connection_id)
            if connection_service
            else None
        )
        if connection is None:
            await query.answer("Аккаунт не найден", show_alert=True)
            return
        async with events.session_factory() as session:
            employee = (
                await session.get(Employee, connection.assigned_employee_id)
                if connection.assigned_employee_id
                else None
            )
            dialogs = int(
                await session.scalar(
                    select(func.count(TelegramDialog.id)).where(
                        TelegramDialog.connection_id == connection.id,
                        TelegramDialog.dialog_type == "personal",
                        TelegramDialog.excluded.is_(False),
                    )
                )
                or 0
            )
            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            messages = int(
                await session.scalar(
                    select(func.count(TelegramMessage.id)).where(
                        TelegramMessage.connection_id == connection.id,
                        TelegramMessage.sent_at >= today,
                        TelegramMessage.deleted_at.is_(None),
                    )
                )
                or 0
            )
            contacts = int(
                await session.scalar(
                    select(func.count(TelegramDialog.id)).where(
                        TelegramDialog.connection_id == connection.id,
                        TelegramDialog.dialog_type == "personal",
                        TelegramDialog.created_at >= today,
                    )
                )
                or 0
            )
        await edit_screen(
            query,
            f"<b>{escape(connection.display_name or connection.phone_masked or 'Telegram')}</b>\n\n"
            f"<blockquote>Сотрудник: <b>{escape(employee.display_name if employee else 'общий аккаунт')}</b>\nСтатус: <b>{escape(connection.status)}</b>\nЛичных диалогов: <b>{dialogs}</b>\nСообщений сегодня: <b>{messages}</b>\nНовых контактов сегодня: <b>{contacts}</b>\nПоследняя синхронизация: {connection.last_sync_at.strftime('%d.%m.%Y %H:%M') if connection.last_sync_at else 'ещё не было'}</blockquote>",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отключить аккаунт", callback_data=f"ctd:{connection.id}"
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

    @router.callback_query(F.data.startswith("ctd:"))
    async def connection_disconnect_confirm(
        query: CallbackQuery, client_context: ClientContext
    ) -> None:
        connection_id = (query.data or "").split(":", 1)[1]
        connection = (
            await connection_service.get(client_context.tenant_id, connection_id)
            if connection_service
            else None
        )
        if connection is None:
            await query.answer("Аккаунт не найден", show_alert=True)
            return
        await edit_screen(
            query,
            "<b>Отключить этот аккаунт?</b>\n\nСессия будет удалена после подтверждения.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Подтвердить отключение", callback_data=f"ctdc:{connection.id}"
                        )
                    ],
                    [InlineKeyboardButton(text="← Назад", callback_data=f"ctv:{connection.id}")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("ctdc:"))
    async def connection_disconnect(query: CallbackQuery, client_context: ClientContext) -> None:
        connection_id = (query.data or "").split(":", 1)[1]
        if connection_service:
            await connection_service.disconnect(client_context.tenant_id, connection_id)
        await connections(query, client_context)

    @router.callback_query(F.data == "client:tg:intro")
    async def connection_intro(query: CallbackQuery) -> None:
        await edit_screen(
            query,
            "<b>Безопасное подключение рабочего Telegram</b>\n\n"
            "1. Отправьте номер рабочего Telegram-аккаунта.\n"
            "2. Введите одноразовый код и, если включена, пароль 2FA.\n"
            "3. Личные рабочие диалоги подключатся автоматически.\n\n"
            "Код и пароль удаляются сразу после проверки и не сохраняются. Сессия хранится "
            "в зашифрованном виде.\n\n"
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
            "<b>Шаг 1 из 3 · Телефон</b>\n\n"
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
            "<b>Шаг 2 из 3 · Код Telegram</b>\n\n"
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

    async def activate_connected_account(
        state: FSMContext,
        bot: Any,
        context: ClientContext,
    ) -> None:
        if connection_service is None:
            return
        data = await state.get_data()
        connection_id = str(data["connection_id"])
        await connection_service.refresh_catalog(context.tenant_id, connection_id)
        async with connection_service.session_factory() as session:
            tenant_settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == context.tenant_id)
            )
        history_days = tenant_settings.message_history_days if tenant_settings else 14
        connection = await connection_service.activate_default_scope(
            context.tenant_id,
            history_days=history_days,
            connection_id=connection_id,
        )
        run = await connection_service.start_initial_sync(
            context.tenant_id,
            connection_id=connection.id,
        )
        await record(context, "telegram_connection_completed")
        await record(context, "initial_sync_started", run_id=run.id)
        await edit_saved_screen(
            state,
            bot,
            "<b>Аккаунт подключён</b>\n\n"
            f"{escape(connection.display_name or connection.phone_masked or 'Telegram')} готов.\n"
            "Личные рабочие диалоги уже добавлены. Первичный анализ продолжится в фоне.\n\n"
            "Рабочие группы можно подключить отдельно в Mini App.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📊 Прогресс", callback_data="client:tg:progress")],
                    [InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")],
                ]
            ),
        )
        await state.clear()

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
                "<b>Шаг 3 из 3 · Пароль 2FA</b>\n\n"
                "Отправьте облачный пароль Telegram. Он будет немедленно удалён и не сохранится.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✕ Отменить", callback_data="client:tg:cancel")]
                    ]
                ),
            )
            return
        try:
            await activate_connected_account(state, message.bot, client_context)
        except Exception as exc:  # noqa: BLE001 - catalog/sync can safely be retried
            await record(
                client_context, "telegram_activation_failed", error_type=type(exc).__name__
            )
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Аккаунт подключён</b>\n\n"
                "Первичная синхронизация пока не запустилась. Нажмите «Прогресс» через минуту.",
                connection_actions("connected"),
            )

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
        try:
            await activate_connected_account(state, message.bot, client_context)
        except Exception as exc:  # noqa: BLE001 - catalog/sync can safely be retried
            await record(
                client_context, "telegram_activation_failed", error_type=type(exc).__name__
            )
            await edit_saved_screen(
                state,
                message.bot,
                "<b>Аккаунт подключён</b>\n\n"
                "Первичная синхронизация пока не запустилась. Нажмите «Прогресс» через минуту.",
                connection_actions("connected"),
            )

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
        await edit_screen(
            query,
            f"<b>Настройки проекта</b>\n\n"
            f"<blockquote><b>Компания</b>\n{escape(tenant.name)}\n\n"
            f"<b>Направление</b>\n{escape(tenant.niche)}\n\n"
            f"<b>Расписание</b>\nРабочие часы: {escape(_hours(tenant.settings.working_hours))}\n"
            f"Плановое время ответа: {tenant.settings.response_sla_minutes} мин.\n"
            f"Сводка: {tenant.settings.daily_report_time:%H:%M} · {escape(tenant.settings.timezone)}</blockquote>\n\n"
            f"<blockquote><b>Уведомления</b>\nСоздавать ситуацию: {tenant.settings.signal_problem_threshold}/100\n"
            f"Присылать срочно: {tenant.settings.signal_immediate_threshold}/100\n"
            f"Сотрудникам: {'включены' if tenant.settings.employee_notifications_enabled else 'выключены'}\n"
            f"В группы: {'включены' if tenant.settings.group_reminders_enabled else 'выключены'}</blockquote>\n\n"
            f"<blockquote><b>Доступ к системе</b>\n{escape(_access_status(tenant))}</blockquote>\n\n<i>Изменить расписание и чувствительность можно в Mini App.</i>",
            settings_markup(),
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
            f"{'✅' if item.status == 'active' else '⏸'} <b>{escape(item.display_name)}</b>\n   @{escape(item.telegram_username or 'username не указан')} · {'доступ связан' if item.telegram_user_id else 'ожидает первого входа'}"
            for item in rows
        ]
        await edit_screen(
            query,
            "<b>👥 Команда</b>\n\n<blockquote>"
            + ("\n\n".join(lines) if lines else "Сотрудники ещё не добавлены.")
            + "</blockquote>\n\n<i>Новый сотрудник добавляется по номеру телефона в Ventrix AI; профиль определяется после входа.</i>",
            settings_markup(),
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
