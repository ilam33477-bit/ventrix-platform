from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.api.deepseek import DeepSeekProvider

from ..config import Settings
from ..jobs.queue import SQLiteJobQueue
from ..models import (
    AIUsageCall,
    AIUsageMetric,
    AnalysisRun,
    BackgroundJob,
    Employee,
    GroupIntegration,
    NotificationLog,
    OperationalProblem,
    OwnerClientDraft,
    ProductEvent,
    Report,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    Tenant,
    TenantAnalysisSchedule,
    TenantSettings,
)
from ..scheduler.service import TenantAnalysisScheduler
from ..schemas import AIProfileUpdate, BotCreate, TenantCreate, TenantUpdate
from ..services.client_drafts import ClientDraftData, OwnerClientDraftService
from ..services.encryption import EncryptionService
from ..services.foundation import BotAlreadyExistsError, FoundationService
from ..services.onboarding_welcome import ensure_onboarding_welcome
from ..services.product_events import ProductEventService
from ..services.system_secrets import SystemSecretService, mask_secret
from ..services.telegram import BotTokenVerificationError, TelegramBotVerifier
from ..telegram_sessions.gateway import TelethonGateway
from ..telegram_sessions.service import TelegramConnectionService
from .keyboards import (
    access_actions,
    ai_draft_confirmation,
    ai_draft_field_selector,
    ai_draft_start,
    ai_profile_actions,
    ai_recommendation_choice,
    back_to_owner_menu,
    bot_actions,
    cancel_flow,
    create_field_selector,
    delete_confirmation,
    edit_confirmation,
    flow_confirmation,
    optional_access_end,
    optional_username,
    owner_main_menu,
    system_secret_actions,
    system_secret_confirmation,
    system_settings_menu,
    tenant_actions,
    tenant_bot_missing,
    tenant_edit_selector,
    tenant_selector,
    test_reset_menu,
)
from .states import (
    AIProfileStates,
    BotCreateStates,
    BotRotateStates,
    SystemSecretStates,
    TenantAccessStates,
    TenantAICreateStates,
    TenantCreateStates,
    TenantEditStates,
    TenantHistoryStates,
)


async def precompute_onboarding_welcome(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
    tenant_id: str,
) -> None:
    if not settings.deepseek_api_key:
        return
    try:
        async with session_factory() as session:
            tenant = await service_for(session, settings, telegram_verifier).get_tenant(tenant_id)
            provider = DeepSeekProvider(
                base_url=settings.deepseek_base_url,
                timeout_seconds=min(30, settings.ai_request_timeout_seconds),
                api_key_value=settings.deepseek_api_key.get_secret_value(),
            )
            await ensure_onboarding_welcome(
                session,
                tenant,
                provider=provider,
                model=settings.deepseek_fast_model,
            )
    except Exception as exc:  # noqa: BLE001 - tenant creation must not depend on AI availability
        logger.warning(
            "Could not precompute onboarding welcome tenant_id=%s error=%s",
            tenant_id,
            type(exc).__name__,
        )


router = Router(name="owner-admin-inline")
logger = logging.getLogger(__name__)

DEFAULT_AI_RECOMMENDATIONS = (
    "Обращай особое внимание на лидов и действующих клиентов без ответа, нарушенные "
    "обещания, повторные напоминания, сорванные созвоны, запросы цены и кейсов, жалобы, "
    "возвраты и обсуждение крупных сделок. Не считай обычную паузу вне рабочего времени "
    "проблемой. Каждый вывод подтверждай конкретным сообщением, временем и username."
)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def service_for(
    session: AsyncSession,
    settings: Settings,
    verifier: TelegramBotVerifier,
    source: str = "owner_bot",
) -> FoundationService:
    return FoundationService(
        session,
        settings.platform_owner_telegram_id,
        EncryptionService(settings.app_encryption_key.get_secret_value()),
        verifier,
        source,
        settings.platform_owner_telegram_username,
    )


async def render(query: CallbackQuery, text_value: str, markup: InlineKeyboardMarkup) -> None:
    if query.message is None:
        await query.answer()
        return
    try:
        await query.message.edit_text(text_value, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await query.answer()


async def begin_flow(
    query: CallbackQuery,
    state: FSMContext,
    state_value: Any,
    flow_kind: str,
    prompt: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    await state.clear()
    await state.set_state(state_value)
    if query.message:
        await state.update_data(
            flow_kind=flow_kind,
            screen_chat_id=query.message.chat.id,
            screen_message_id=query.message.message_id,
        )
    await render(query, prompt, markup or cancel_flow())


async def update_flow_screen(
    message: Message,
    state: FSMContext,
    prompt: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    data = await state.get_data()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - compact UI cleanup is best-effort
        logger.debug("Could not remove an owner flow input message")
    try:
        await message.bot.edit_message_text(
            chat_id=data["screen_chat_id"],
            message_id=data["screen_message_id"],
            text=prompt,
            reply_markup=markup or cancel_flow(),
        )
    except (KeyError, TelegramBadRequest):
        await message.answer(prompt, reply_markup=markup or cancel_flow())


def normalize_username(raw: str) -> str | None:
    value = raw.strip().lstrip("@").lower()
    if value.lower() in {"нет", "none", "-", "пропустить"}:
        return None
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("username должен содержать 5–32 латинских символа, цифры или _")
    return value


def parse_user_id(raw: str) -> int:
    value = raw.strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("Telegram user ID должен быть положительным числом")
    return int(value)


def parse_access_date(raw: str) -> date | None:
    value = raw.strip().lower()
    if value in {"без срока", "нет", "none", "-"}:
        return None
    if len(value) != 10:
        raise ValueError("дата должна быть в формате ГГГГ-ММ-ДД")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("дата должна быть в формате ГГГГ-ММ-ДД") from exc
    if parsed < datetime.now(UTC).date():
        raise ValueError("дата окончания не может быть в прошлом")
    return parsed


def restore_access_date(value: Any) -> date | None:
    """Restore the ISO value persisted by the JSON-backed FSM."""
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("subscription_expires_at must be an ISO date string")


def validate_field(field: str, raw: str) -> Any:
    value = raw.strip()
    if field == "owner_telegram_user_id":
        return parse_user_id(value)
    if field == "owner_telegram_username":
        return normalize_username(value)
    if field == "subscription_expires_at":
        return parse_access_date(value)
    limits = {
        "name": (2, 200),
        "niche": (2, 200),
        "target_audience": (2, 10_000),
        "additional_ai_instructions": (10, 20_000),
    }
    minimum, maximum = limits[field]
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"поле должно содержать от {minimum} до {maximum} символов")
    return value


def access_text(tenant: Any) -> str:
    if tenant.status != "active":
        return "приостановлен"
    if tenant.subscription_expires_at is None:
        return "активен без указанной даты окончания"
    days = (tenant.subscription_expires_at - datetime.now(UTC).date()).days
    if days < 0:
        return f"истёк {tenant.subscription_expires_at:%d.%m.%Y}"
    return f"активен до {tenant.subscription_expires_at:%d.%m.%Y} · осталось {days} дн."


def tenant_card(tenant: Any) -> str:
    bots = [bot for bot in tenant.bots if bot.deleted_at is None]
    client_bot = f"@{bots[0].username}" if bots else "не подключён"
    last_usage = max((bot.last_update_at for bot in bots if bot.last_update_at), default=None)
    owner = escape(tenant.owner_telegram_username or str(tenant.owner_telegram_user_id))
    return (
        f"<b>Карточка клиента</b>\n\n"
        f"Компания: <b>{escape(tenant.name)}</b>\n"
        f"Ниша: {escape(tenant.niche)}\n"
        f"Владелец: {owner}\n"
        f"Клиентский бот: {escape(client_bot)}\n"
        f"Доступ: {escape(access_text(tenant))}\n"
        "Telegram-аккаунты: 0\n"
        f"Последнее использование: {last_usage.isoformat() if last_usage else '—'}\n"
        "Открытые проблемы: 0\n"
        "Последняя отчётность: —"
    )


async def tenant_operational_card(tenant: Any, session: AsyncSession) -> str:
    connections = list(
        await session.scalars(
            select(TelegramConnection)
            .where(
                TelegramConnection.tenant_id == tenant.id,
                TelegramConnection.deleted_at.is_(None),
            )
            .order_by(TelegramConnection.created_at.desc())
        )
    )
    connection = connections[0] if connections else None
    schedule = await session.scalar(
        select(TenantAnalysisSchedule).where(TenantAnalysisSchedule.tenant_id == tenant.id)
    )
    job = await session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.tenant_id == tenant.id,
            BackgroundJob.status.in_(("pending", "scheduled", "waiting", "retry", "running")),
        )
        .order_by(BackgroundJob.created_at.desc())
        .limit(1)
    )
    messages = int(
        await session.scalar(
            select(func.count(TelegramMessage.id)).where(TelegramMessage.tenant_id == tenant.id)
        )
    )
    problems = int(
        await session.scalar(
            select(func.count(OperationalProblem.id)).where(
                OperationalProblem.tenant_id == tenant.id,
                OperationalProblem.status.in_(("open", "needs_confirmation")),
            )
        )
    )
    report = await session.scalar(
        select(Report)
        .where(Report.tenant_id == tenant.id)
        .order_by(Report.period_end.desc())
        .limit(1)
    )
    usage = await session.execute(
        select(
            func.coalesce(func.sum(AIUsageMetric.input_tokens), 0),
            func.coalesce(func.sum(AIUsageMetric.output_tokens), 0),
            func.coalesce(func.sum(AIUsageMetric.request_count), 0),
        ).where(AIUsageMetric.tenant_id == tenant.id)
    )
    input_tokens, output_tokens, requests = usage.one()
    base = tenant_card(tenant)
    return (
        base.replace("Telegram-аккаунты: 0", f"Telegram-аккаунты: {len(connections)}")
        .replace("Открытые проблемы: 0", f"Открытые проблемы: {problems}")
        .replace(
            "Последняя отчётность: —",
            f"Последняя отчётность: {report.ready_at.isoformat() if report and report.ready_at else '—'}",
        )
        + "\n\n<b>Операционный контур</b>\n"
        + "Сессии: "
        + escape(
            ", ".join(item.health_status for item in connections)
            if connections
            else "не подключены"
        )
        + "\n"
        + f"Папки: {escape(connection.selected_folder_title if connection and connection.selected_folder_title else '—')}\n"
        + f"Последняя синхронизация: {connection.last_sync_at.isoformat() if connection and connection.last_sync_at else '—'}\n"
        + f"Следующий анализ: {schedule.next_analysis_at.isoformat() if schedule and schedule.next_analysis_at else '—'}\n"
        + f"Текущая задача: {escape(job.job_type + ' · ' + job.status) if job else '—'}\n"
        + f"Прогресс: {escape(str(job.progress_json)) if job else '—'}\n"
        + f"Сообщения: {messages}\n"
        + f"AI usage: {input_tokens + output_tokens} tokens · {requests} requests"
    )


def bot_card(bot: Any, tenant_name: str, unique_users: int = 0, miniapp_opens: int = 0) -> str:
    return (
        "<b>Клиентский бот</b>\n\n"
        f"Название: {escape(bot.display_name)}\n"
        f"Username: @{escape(bot.username)}\n"
        f"Клиент: {escape(tenant_name)}\n"
        f"Runtime: {escape(bot.runtime_status)}\n"
        f"Последний запуск: {bot.last_started_at.isoformat() if bot.last_started_at else '—'}\n"
        f"Последнее событие: {bot.last_update_at.isoformat() if bot.last_update_at else '—'}\n"
        f"Пользователи: {unique_users}\n"
        f"Открытия панели: {miniapp_opens}\n"
        f"Ошибки: {escape(bot.last_error or 'нет')}"
    )


def review_text(data: dict[str, Any]) -> str:
    recommendations = escape(str(data.get("additional_ai_instructions") or "—"))
    end = data.get("subscription_expires_at")
    end_text = restore_access_date(end).isoformat() if end is not None else "без ограничения"
    username = data.get("owner_telegram_username")
    return (
        "<b>Проверьте данные перед созданием</b>\n\n"
        f"Компания: {escape(str(data.get('name', '—')))}\n"
        f"Владелец: @{escape(username) if username else 'не указан'}\n"
        f"Telegram user ID: <code>{data.get('owner_telegram_user_id', '—')}</code>\n"
        f"Ниша: {escape(str(data.get('niche', '—')))}\n"
        f"Целевая аудитория: {escape(str(data.get('target_audience', '—')))}\n"
        f"AI-рекомендации: {recommendations}\n"
        f"Доступ до: {escape(end_text)}\n"
        "Клиентский бот: подключается после создания из карточки клиента\n\n"
        "До нажатия «Подтвердить» данные не сохраняются."
    )


async def show_owner_menu(query: CallbackQuery) -> None:
    await render(
        query,
        "<b>Управление проектами</b>\n\nВыберите раздел.",
        owner_main_menu(),
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    if await state.get_state():
        await message.answer("Сначала завершите действие или используйте /cancel.")
        return
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        await service.ensure_owner(message.from_user.username if message.from_user else None)
        await session.commit()
    notice = await message.answer("Обновляю меню…", reply_markup=ReplyKeyboardRemove())
    try:
        await notice.delete()
    except Exception:  # noqa: BLE001 - one-time reply-keyboard cleanup is best-effort
        logger.debug("Could not remove the reply-keyboard migration notice")
    await message.answer(
        "<b>Управление проектами</b>\n\nВыберите раздел.", reply_markup=owner_main_menu()
    )


@router.callback_query(F.data == "owner:menu")
async def owner_menu_callback(query: CallbackQuery) -> None:
    await show_owner_menu(query)


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Действие отменено. Временные данные очищены.", reply_markup=owner_main_menu()
    )


@router.callback_query(F.data == "flow:cancel")
async def cancel_callback(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render(query, "Действие отменено. Временные данные очищены.", owner_main_menu())


@router.callback_query(F.data == "flow:restart")
async def restart_flow(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("flow_kind") != "tenant_create":
        await state.clear()
        await show_owner_menu(query)
        return
    screen = {key: data[key] for key in ("screen_chat_id", "screen_message_id") if key in data}
    await state.clear()
    await state.set_state(TenantAICreateStates.owner_name)
    await state.update_data(flow_kind="tenant_create_ai", **screen)
    await render(query, "<b>Новый клиент · 1/4</b>\n\nВведите имя клиента.", cancel_flow())


@router.callback_query(F.data == "owner:tenant_create")
async def tenant_create_start(query: CallbackQuery, state: FSMContext) -> None:
    await begin_flow(
        query,
        state,
        TenantAICreateStates.owner_name,
        "tenant_create_ai",
        "<b>Новый клиент · 1/4</b>\n\nВведите имя клиента.\n\nНапример: <code>Вадим</code>",
    )


@router.callback_query(F.data == "owner:tenant_create:manual")
async def tenant_create_manual(query: CallbackQuery, state: FSMContext) -> None:
    await begin_flow(
        query,
        state,
        TenantCreateStates.name,
        "tenant_create",
        "<b>Новый клиент · 1/7</b>\n\nВведите название компании.",
    )


@router.callback_query(F.data == "owner:tenant_create:ai")
async def tenant_create_ai(query: CallbackQuery, state: FSMContext) -> None:
    await begin_flow(
        query,
        state,
        TenantAICreateStates.owner_name,
        "tenant_create_ai",
        "<b>Новый клиент · 1/4</b>\n\nВведите имя клиента.",
    )


@router.message(TenantAICreateStates.owner_name)
async def tenant_ai_owner_name(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 2 or len(value) > 200:
        await update_flow_screen(message, state, "Введите имя длиной от 2 до 200 символов.")
        return
    await state.update_data(owner_name=value)
    await state.set_state(TenantAICreateStates.owner_user_id)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 2/4</b>\n\nВведите числовой Telegram user ID клиента.\n\n"
        "Username указывать не нужно.",
    )


@router.message(TenantAICreateStates.owner_user_id)
async def tenant_ai_owner_id(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        user_id = parse_user_id(message.text or "")
    except ValueError:
        await update_flow_screen(message, state, "Введите Telegram ID только цифрами.")
        return
    username = None
    try:
        chat = await bot.get_chat(user_id)
        username = chat.username
    except TelegramAPIError:
        # A Telegram user may not have opened the owner bot. Username remains optional.
        pass
    await state.update_data(
        owner_telegram_user_id=user_id,
        owner_telegram_username=username,
    )
    await state.set_state(TenantAICreateStates.company_name)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 3/4</b>\n\nВведите название компании или проекта.",
    )


@router.message(TenantAICreateStates.company_name)
async def tenant_ai_company(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 2 or len(value) > 200:
        await update_flow_screen(message, state, "Введите название длиной от 2 до 200 символов.")
        return
    await state.update_data(name=value)
    await state.set_state(TenantAICreateStates.ai_choice)
    await update_flow_screen(
        message,
        state,
        "<b>Основные данные готовы</b>\n\n"
        "Теперь можно описать бизнес свободным текстом. Ventrix заполнит остальные настройки "
        "и сначала покажет черновик.",
        ai_draft_start(),
    )


@router.callback_query(TenantAICreateStates.ai_choice, F.data == "flow:ai_draft:describe")
async def tenant_ai_description_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TenantAICreateStates.prompt)
    await render(
        query,
        "<b>Опишите клиента одним сообщением</b>\n\n"
        "Можно написать в свободной форме. Ventrix разберёт описание и покажет черновик.\n\n"
        "<pre>Компания занимается AI-рассылками в Telegram.\n\n"
        "Ниша:\nB2B автоматизация продаж.\n\n"
        "Целевая аудитория:\nмалый и средний бизнес, руководители отделов продаж.\n\n"
        "Основные риски:\nклиенты без ответа, просроченные обещания, счета, жалобы.\n\n"
        "SLA:\n60 минут.\n\n"
        "Регулярный отчёт:\nпо будням в 19:00 по Москве.\n\n"
        "История анализа:\nактивные диалоги за последние 30 дней, сообщения внутри них за 14 дней.</pre>\n\n"
        "Не отправляйте сюда пароли, API-ключи и bot token.",
        cancel_flow(),
    )


def ai_draft_text(data: ClientDraftData, version: int) -> str:
    username = f"@{data.owner_telegram_username}" if data.owner_telegram_username else "—"
    return (
        f"<b>Черновик клиента · v{version}</b>\n\n"
        f"Компания: <b>{escape(data.name)}</b>\n"
        f"Владелец: {escape(data.owner_name)} · {data.owner_telegram_user_id} · {escape(username)}\n"
        f"Ниша: {escape(data.niche)}\n"
        f"Продукты: {escape(data.products_services)}\n"
        f"Аудитория: {escape(data.target_audience)}\n"
        f"Timezone: {escape(data.timezone)}\n"
        f"SLA ответа: {data.response_sla_minutes} мин.\n"
        f"Отчёт: {escape(data.daily_report_time)}\n\n"
        f"Критичные события:\n{escape(data.critical_problem_criteria)}\n\n"
        "Проверьте данные. До подтверждения клиент в БД не создаётся."
    )


def ai_draft_service(session: AsyncSession, settings: Settings) -> OwnerClientDraftService:
    if not settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API is not configured")
    return OwnerClientDraftService(
        session,
        DeepSeekProvider(
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.ai_request_timeout_seconds,
            api_key_value=settings.deepseek_api_key.get_secret_value(),
        ),
        EncryptionService(settings.app_encryption_key.get_secret_value()),
        settings.deepseek_fast_model,
    )


@router.message(TenantAICreateStates.prompt)
async def tenant_ai_prompt(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    prompt = (message.text or "").strip()
    if len(prompt) < 20:
        await update_flow_screen(message, state, "Опишите клиента подробнее — минимум 20 символов.")
        return
    try:
        flow_data = await state.get_data()
        identity = {
            "owner_name": flow_data["owner_name"],
            "owner_telegram_user_id": flow_data["owner_telegram_user_id"],
            "owner_telegram_username": flow_data.get("owner_telegram_username"),
            "name": flow_data["name"],
        }
        async with session_factory() as session:
            draft = await ai_draft_service(session, settings).create(
                settings.platform_owner_telegram_id, prompt, identity=identity
            )
    except Exception as exc:
        logger.exception("Owner AI client draft failed")
        await update_flow_screen(
            message, state, f"Не удалось собрать черновик: {escape(str(exc))}", cancel_flow()
        )
        return
    await state.update_data(draft_id=draft.id)
    await state.set_state(TenantAICreateStates.confirm)
    await update_flow_screen(
        message,
        state,
        ai_draft_text(ClientDraftData.model_validate(draft.draft_json), draft.version),
        ai_draft_confirmation(),
    )


@router.callback_query(TenantAICreateStates.confirm, F.data == "flow:ai_draft:correct")
async def tenant_ai_correction_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TenantAICreateStates.correction)
    await render(query, "Опишите одним сообщением, что изменить в черновике.", cancel_flow())


@router.callback_query(TenantAICreateStates.confirm, F.data == "flow:ai_draft:fields")
async def tenant_ai_field_selector(query: CallbackQuery) -> None:
    await render(query, "Какое поле изменить?", ai_draft_field_selector())


@router.callback_query(F.data.startswith("flow:ai_draft:field:"))
async def tenant_ai_field_start(query: CallbackQuery, state: FSMContext) -> None:
    field = query.data.rsplit(":", 1)[-1]
    await state.update_data(correction_field=field)
    await state.set_state(TenantAICreateStates.correction)
    await render(query, f"Введите новое значение поля <code>{escape(field)}</code>.", cancel_flow())


@router.callback_query(TenantAICreateStates.confirm, F.data == "flow:ai_draft:back")
async def tenant_ai_draft_back(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    async with session_factory() as session:
        draft = await session.get(OwnerClientDraft, str(data["draft_id"]))
    if draft is None:
        raise LookupError("Client draft not found")
    await render(
        query,
        ai_draft_text(ClientDraftData.model_validate(draft.draft_json), draft.version),
        ai_draft_confirmation(),
    )


@router.message(TenantAICreateStates.correction)
async def tenant_ai_correction(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    data = await state.get_data()
    correction = (message.text or "").strip()
    if field := data.get("correction_field"):
        correction = f"Измени только поле {field}. Новое значение: {correction}"
    try:
        async with session_factory() as session:
            draft = await ai_draft_service(session, settings).correct(
                settings.platform_owner_telegram_id,
                str(data["draft_id"]),
                correction,
            )
    except Exception as exc:
        logger.exception("Owner AI client draft correction failed")
        await update_flow_screen(message, state, f"Исправление не применено: {escape(str(exc))}")
        return
    await state.set_state(TenantAICreateStates.confirm)
    await state.update_data(correction_field=None)
    await update_flow_screen(
        message,
        state,
        ai_draft_text(ClientDraftData.model_validate(draft.draft_json), draft.version),
        ai_draft_confirmation(),
    )


@router.callback_query(TenantAICreateStates.confirm, F.data == "flow:ai_draft:confirm")
async def tenant_ai_confirm(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    data = await state.get_data()
    async with session_factory() as session:
        draft = await session.get(OwnerClientDraft, str(data["draft_id"]))
        if draft is None:
            raise LookupError("Client draft not found")
        if draft.created_tenant_id:
            tenant = await service_for(session, settings, telegram_verifier).get_tenant(
                draft.created_tenant_id
            )
        else:
            payload = ClientDraftData.model_validate(draft.draft_json).tenant_payload()
            foundation = service_for(session, settings, telegram_verifier)
            tenant = await foundation.create_tenant(payload, commit=False)
            draft.created_tenant_id = tenant.id
            draft.status = "confirmed"
            draft.confirmed_at = datetime.now(UTC)
            await session.commit()
            tenant = await foundation.get_tenant(tenant.id)
    await precompute_onboarding_welcome(session_factory, settings, telegram_verifier, tenant.id)
    await state.clear()
    await render(
        query,
        "Клиент создан из подтверждённого AI-черновика.\n\n" + tenant_card(tenant),
        tenant_actions(tenant.id, suspended=tenant.status != "active"),
    )


@router.message(TenantCreateStates.name)
async def tenant_name(message: Message, state: FSMContext) -> None:
    try:
        value = validate_field("name", message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nВведите название компании."
        )
        return
    await state.update_data(name=value)
    await state.set_state(TenantCreateStates.owner_user_id)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 2/7</b>\n\nВведите числовой Telegram user ID владельца.",
    )


@router.message(TenantCreateStates.owner_user_id)
async def tenant_owner_id(message: Message, state: FSMContext) -> None:
    try:
        value = parse_user_id(message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nВведите только число."
        )
        return
    await state.update_data(owner_telegram_user_id=value)
    await state.set_state(TenantCreateStates.owner_username)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 3/7</b>\n\nВведите Telegram username владельца без @ или пропустите.",
        optional_username(),
    )


@router.callback_query(TenantCreateStates.owner_username, F.data == "flow:username:skip")
async def tenant_username_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(owner_telegram_username=None)
    await state.set_state(TenantCreateStates.niche)
    await render(query, "<b>Новый клиент · 4/7</b>\n\nУкажите нишу компании.", cancel_flow())


@router.message(TenantCreateStates.owner_username)
async def tenant_username(message: Message, state: FSMContext) -> None:
    try:
        value = normalize_username(message.text or "")
    except ValueError as exc:
        await update_flow_screen(message, state, f"Ошибка: {escape(str(exc))}", optional_username())
        return
    await state.update_data(owner_telegram_username=value)
    await state.set_state(TenantCreateStates.niche)
    await update_flow_screen(message, state, "<b>Новый клиент · 4/7</b>\n\nУкажите нишу компании.")


@router.message(TenantCreateStates.niche)
async def tenant_niche(message: Message, state: FSMContext) -> None:
    try:
        value = validate_field("niche", message.text or "")
    except ValueError as exc:
        await update_flow_screen(message, state, f"Ошибка: {escape(str(exc))}\n\nУкажите нишу.")
        return
    await state.update_data(niche=value)
    await state.set_state(TenantCreateStates.audience)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 5/7</b>\n\nОпишите целевую аудиторию.\nНапример: владельцы и операционные директора розничных сетей.",
    )


@router.message(TenantCreateStates.audience)
async def tenant_audience(message: Message, state: FSMContext) -> None:
    try:
        value = validate_field("target_audience", message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nОпишите аудиторию."
        )
        return
    await state.update_data(target_audience=value)
    await state.set_state(TenantCreateStates.ai_choice)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 6/7</b>\n\nВыберите рекомендации для анализа.\n\n"
        f"<i>{escape(DEFAULT_AI_RECOMMENDATIONS)}</i>",
        ai_recommendation_choice(),
    )


@router.callback_query(TenantCreateStates.ai_choice, F.data == "flow:ai:default")
async def ai_default(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(additional_ai_instructions=DEFAULT_AI_RECOMMENDATIONS)
    await state.set_state(TenantCreateStates.access_end)
    await render(
        query,
        "<b>Новый клиент · 7/7</b>\n\nВведите дату окончания доступа в формате ГГГГ-ММ-ДД.",
        optional_access_end(),
    )


@router.callback_query(TenantCreateStates.ai_choice, F.data == "flow:ai:generate")
async def ai_generate(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    generated = (
        f"Для ниши «{data.get('niche')}» и аудитории «{data.get('target_audience')}» "
        + DEFAULT_AI_RECOMMENDATIONS
    )
    await state.update_data(additional_ai_instructions=generated)
    await state.set_state(TenantCreateStates.access_end)
    await render(
        query,
        "Сформирован черновик рекомендаций на основе ниши. Его можно изменить перед сохранением.\n\n"
        "Введите дату окончания доступа в формате ГГГГ-ММ-ДД.",
        optional_access_end(),
    )


@router.callback_query(TenantCreateStates.ai_choice, F.data == "flow:ai:custom")
async def ai_custom_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TenantCreateStates.ai_custom)
    await render(query, "Введите свои рекомендации для анализа.", cancel_flow())


@router.message(TenantCreateStates.ai_custom)
async def ai_custom_finish(message: Message, state: FSMContext) -> None:
    try:
        value = validate_field("additional_ai_instructions", message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nВведите рекомендации."
        )
        return
    await state.update_data(additional_ai_instructions=value)
    await state.set_state(TenantCreateStates.access_end)
    await update_flow_screen(
        message,
        state,
        "<b>Новый клиент · 7/7</b>\n\nВведите дату окончания доступа в формате ГГГГ-ММ-ДД.",
        optional_access_end(),
    )


async def show_create_review(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TenantCreateStates.confirm)
    await render(query, review_text(await state.get_data()), flow_confirmation())


@router.callback_query(TenantCreateStates.access_end, F.data == "flow:access:none")
async def access_none(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(subscription_expires_at=None)
    await show_create_review(query, state)


@router.message(TenantCreateStates.access_end)
async def access_end(message: Message, state: FSMContext) -> None:
    try:
        value = parse_access_date(message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}", optional_access_end()
        )
        return
    await state.update_data(
        subscription_expires_at=value.isoformat() if value is not None else None
    )
    await state.set_state(TenantCreateStates.confirm)
    await update_flow_screen(
        message, state, review_text(await state.get_data()), flow_confirmation()
    )


@router.callback_query(TenantCreateStates.confirm, F.data == "flow:change")
async def create_change(query: CallbackQuery) -> None:
    await render(query, "Какое поле изменить?", create_field_selector())


@router.callback_query(F.data.startswith("flow:change:"))
async def create_change_field(query: CallbackQuery, state: FSMContext) -> None:
    field = query.data.split(":", 2)[2]
    await state.update_data(edit_field=field)
    await state.set_state(TenantCreateStates.edit_value)
    await render(query, f"Введите новое значение поля «{escape(field)}».", cancel_flow())


@router.message(TenantCreateStates.edit_value)
async def create_change_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["edit_field"]
    try:
        value = validate_field(field, message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nВведите значение ещё раз."
        )
        return
    stored_value = value.isoformat() if field == "subscription_expires_at" and value else value
    await state.update_data(**{field: stored_value})
    await state.set_state(TenantCreateStates.confirm)
    await update_flow_screen(
        message, state, review_text(await state.get_data()), flow_confirmation()
    )


@router.callback_query(F.data == "flow:review")
async def create_review_again(query: CallbackQuery, state: FSMContext) -> None:
    await show_create_review(query, state)


@router.callback_query(TenantCreateStates.confirm, F.data == "flow:confirm")
async def tenant_create_confirm(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    data = await state.get_data()
    payload = TenantCreate(
        name=data["name"],
        owner_name=data.get("owner_telegram_username") or "Владелец проекта",
        owner_telegram_username=data.get("owner_telegram_username"),
        owner_telegram_user_id=data["owner_telegram_user_id"],
        niche=data["niche"],
        business_description=f"Описание будет заполнено клиентом. Ниша: {data['niche']}",
        products_services="Будет заполнено клиентом",
        target_audience=data["target_audience"],
        working_hours={"description": "Будут настроены клиентом"},
        timezone="Europe/Moscow",
        response_sla_minutes=60,
        critical_problem_criteria=data["additional_ai_instructions"],
        daily_report_time=time(9, 0),
        subscription_expires_at=restore_access_date(data.get("subscription_expires_at")),
        additional_ai_instructions=data["additional_ai_instructions"],
    )
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).create_tenant(payload)
    await precompute_onboarding_welcome(session_factory, settings, telegram_verifier, tenant.id)
    await state.clear()
    await render(
        query,
        "Клиент создан.\n\n" + tenant_card(tenant),
        tenant_actions(tenant.id, suspended=tenant.status != "active"),
    )


@router.callback_query(F.data == "owner:clients")
async def clients(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    async with session_factory() as session:
        items = await service_for(session, settings, telegram_verifier).list_tenants()
    if not items:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➕ Создать клиента", callback_data="owner:tenant_create"
                    )
                ],
                [InlineKeyboardButton(text="← Главное меню", callback_data="owner:menu")],
            ]
        )
        await render(query, "Клиентов пока нет.", markup)
        return
    await render(
        query,
        f"<b>Клиенты</b> · {len(items)}\n\nВыберите проект.",
        tenant_selector([(str(item.id), item.name) for item in items], "tenant:view"),
    )


@router.callback_query(F.data.startswith("tenant:view:"))
async def tenant_view(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).get_tenant(tenant_id)
        card = await tenant_operational_card(tenant, session)
    await render(
        query,
        card,
        tenant_actions(tenant.id, suspended=tenant.status != "active"),
    )


@router.callback_query(F.data.startswith("tenant:test_reset:"))
async def tenant_test_reset(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.name.upper().startswith("TEST"):
        await query.answer("Reset доступен только проектам с префиксом TEST", show_alert=True)
        return
    await render(
        query,
        "<b>Сброс тестового клиента</b>\n\n"
        "Onboarding: сохраняет Telegram и business data.\n"
        "Connection + onboarding: безопасно отзывает только сессию Ventrix.\n"
        "Full test reset: также удаляет импортированные данные этого TEST tenant.\n\n"
        "Выберите режим. Действие начнётся только после этой явной команды.",
        test_reset_menu(tenant_id),
    )


@router.callback_query(F.data.startswith("tenant:test_reset_confirm:"))
async def tenant_test_reset_confirm(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    _, _, _, mode, tenant_id = query.data.split(":", 4)
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None or not tenant.name.upper().startswith("TEST"):
            await query.answer("Reset разрешён только TEST tenant", show_alert=True)
            return
        connections = list(
            await session.scalars(
                select(TelegramConnection).where(
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                )
            )
        )
    if mode in {"connection", "full"}:
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            await query.answer("Telegram runtime не настроен", show_alert=True)
            return
        connection_service = TelegramConnectionService(
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
        )
        for connection in connections:
            await connection_service.disconnect(tenant_id, connection.id)
            if mode == "full":
                await connection_service.clear_data(tenant_id, connection.id)

    async with session_factory() as session:
        tenant_settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
        )
        tenant_settings.client_onboarding_step = "welcome"
        tenant_settings.client_onboarding_completed_at = None
        tenant_settings.client_onboarding_json = {}
        if mode in {"connection", "full"}:
            await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.tenant_id == tenant_id,
                    BackgroundJob.status.in_(
                        ("pending", "scheduled", "waiting", "retry", "retry_scheduled")
                    ),
                    BackgroundJob.job_type.in_(
                        (
                            "telegram.sync_chat",
                            "signal.scan_batch",
                            "signal.ai_triage",
                            "notification.initial_summary",
                            "analysis.deep",
                            "analysis.pipeline",
                            "analysis.connection",
                            "analysis.aggregate",
                            "ai_batch_analysis",
                            "report_generation",
                            "report_delivery",
                            "statistics_refresh",
                        )
                    ),
                )
                .values(status="cancelled", finished_at=datetime.now(UTC))
            )
        if mode == "full":
            # Reports/analysis runs are tenant-wide and survive connection FK
            # deletion via SET NULL. Remove them explicitly so a TEST reset
            # cannot show stale summaries for already deleted source data.
            await session.execute(delete(Report).where(Report.tenant_id == tenant_id))
            await session.execute(delete(AnalysisRun).where(AnalysisRun.tenant_id == tenant_id))
        await session.commit()
    await render(
        query,
        f"TEST tenant сброшен в режиме <b>{escape(mode)}</b>.\n\n"
        "Следующий /start и открытие Mini App начнут onboarding заново.",
        tenant_actions(tenant_id),
    )


@router.callback_query(F.data.startswith("tenant:edit:"))
async def tenant_edit(query: CallbackQuery) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    await render(query, "Выберите поле для изменения.", tenant_edit_selector(tenant_id))


@router.callback_query(F.data.startswith("tenant:analysis_now:"))
async def tenant_analysis_now(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    scheduler = TenantAnalysisScheduler(session_factory, SQLiteJobQueue(session_factory))
    job_id = await scheduler.trigger_now(tenant_id)
    await render(
        query,
        f"Проверка поставлена в очередь.\n\nJob: <code>{escape(job_id)}</code>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")]
            ]
        ),
    )


@router.callback_query(F.data.startswith("tenant:analysis_cancel:"))
async def tenant_analysis_cancel(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        job = await session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.tenant_id == tenant_id,
                BackgroundJob.status.in_(("pending", "scheduled", "waiting", "retry", "running")),
            )
            .order_by(BackgroundJob.created_at.desc())
            .limit(1)
        )
    cancelled = bool(
        job and await SQLiteJobQueue(session_factory).cancel(job.id, tenant_id=tenant_id)
    )
    await query.answer("Задача отменена." if cancelled else "Активной задачи нет.", show_alert=True)


@router.callback_query(F.data.startswith("tenant:edit_field:"))
async def tenant_edit_field(query: CallbackQuery, state: FSMContext) -> None:
    _, _, field, tenant_id = query.data.split(":", 3)
    await begin_flow(
        query,
        state,
        TenantEditStates.value,
        "tenant_edit",
        f"Введите новое значение поля «{escape(field)}».",
    )
    await state.update_data(tenant_id=tenant_id, edit_field=field)


@router.message(TenantEditStates.value)
async def tenant_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        value = validate_field(data["edit_field"], message.text or "")
    except ValueError as exc:
        await update_flow_screen(
            message, state, f"Ошибка: {escape(str(exc))}\n\nВведите значение ещё раз."
        )
        return
    stored_value = value.isoformat() if isinstance(value, date) else value
    await state.update_data(proposed_value=stored_value)
    await state.set_state(TenantEditStates.confirm)
    shown = value.isoformat() if isinstance(value, date) else value
    await update_flow_screen(
        message,
        state,
        f"Сохранить новое значение?\n\n<b>{escape(str(shown or 'не указан'))}</b>",
        edit_confirmation(),
    )


@router.callback_query(TenantEditStates.confirm, F.data == "tenant:edit_confirm")
async def tenant_edit_confirm(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    data = await state.get_data()
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        if data["edit_field"] == "subscription_expires_at":
            tenant = await service.set_tenant_access(
                data["tenant_id"], expires_at=restore_access_date(data["proposed_value"])
            )
        else:
            tenant = await service.update_tenant(
                data["tenant_id"], TenantUpdate(**{data["edit_field"]: data["proposed_value"]})
            )
    await state.clear()
    await render(
        query,
        "Изменения сохранены.\n\n" + tenant_card(tenant),
        tenant_actions(tenant.id, suspended=tenant.status != "active"),
    )


@router.callback_query(F.data.startswith("ai:view:"))
async def ai_view(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).get_tenant(tenant_id)
    await render(
        query,
        "<b>AI-настройки</b>\n\n"
        + escape(tenant.ai_profile.additional_instructions or "Не заданы"),
        ai_profile_actions(tenant_id),
    )


@router.callback_query(F.data.startswith("ai:edit:"))
async def ai_edit(query: CallbackQuery, state: FSMContext) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    await begin_flow(
        query,
        state,
        AIProfileStates.recommendations,
        "ai_recommendations",
        "Введите новые рекомендации AI. Сохранение произойдёт после проверки текста.",
    )
    await state.update_data(tenant_id=tenant_id)


@router.message(AIProfileStates.recommendations)
async def ai_edit_finish(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    try:
        value = validate_field("additional_ai_instructions", message.text or "")
    except ValueError as exc:
        await update_flow_screen(message, state, f"Ошибка: {escape(str(exc))}")
        return
    data = await state.get_data()
    async with session_factory() as session:
        await service_for(session, settings, telegram_verifier).update_ai_profile(
            data["tenant_id"], AIProfileUpdate(additional_instructions=value)
        )
    await state.clear()
    try:
        await message.bot.edit_message_text(
            chat_id=data["screen_chat_id"],
            message_id=data["screen_message_id"],
            text="AI-рекомендации сохранены.",
            reply_markup=ai_profile_actions(data["tenant_id"]),
        )
    except TelegramBadRequest:
        await message.answer("AI-рекомендации сохранены.", reply_markup=owner_main_menu())


@router.callback_query(F.data.startswith("tenant:access:"))
async def tenant_access(query: CallbackQuery) -> None:
    await render(query, "Продлить доступ к проекту:", access_actions(query.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("tenant:history:"))
async def tenant_history(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        settings_row = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
        )
    if settings_row is None:
        await query.answer("Настройки клиента не найдены", show_alert=True)
        return
    await render(
        query,
        "<b>История первого анализа</b>\n\n"
        f"Активные диалоги: <b>{settings_row.active_dialog_days} дней</b>\n"
        "Ventrix возьмёт диалоги, где была активность за этот период.\n\n"
        f"Глубина сообщений: <b>{settings_row.message_history_days} дней</b>\n"
        "В каждом актуальном диалоге сообщения анализируются не глубже этого периода.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Изменить активные диалоги",
                        callback_data=f"tenant:history_edit:active:{tenant_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Изменить глубину сообщений",
                        callback_data=f"tenant:history_edit:messages:{tenant_id}",
                    )
                ],
                [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("tenant:history_edit:"))
async def tenant_history_edit(query: CallbackQuery, state: FSMContext) -> None:
    _, _, field, tenant_id = query.data.split(":", 3)
    await begin_flow(
        query,
        state,
        TenantHistoryStates.value,
        "tenant_history",
        "Введите число от 0 до 180.\n\n"
        "0 означает: не загружать старую историю, анализировать только новые события после подключения.",
    )
    await state.update_data(tenant_id=tenant_id, history_field=field)


@router.message(TenantHistoryStates.value)
async def tenant_history_save(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    try:
        value = int((message.text or "").strip())
        if not 0 <= value <= 180:
            raise ValueError
    except ValueError:
        await update_flow_screen(message, state, "Введите целое число от 0 до 180.")
        return
    data = await state.get_data()
    async with session_factory() as session:
        settings_row = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == data["tenant_id"])
        )
        if settings_row is None:
            raise LookupError("Tenant settings not found")
        attribute = (
            "active_dialog_days" if data["history_field"] == "active" else "message_history_days"
        )
        setattr(settings_row, attribute, value)
        await session.commit()
    tenant_id = str(data["tenant_id"])
    await state.clear()
    await message.answer(
        "Настройки истории сохранены.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="← История анализа", callback_data=f"tenant:history:{tenant_id}"
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("tenant:extend:"))
async def tenant_extend(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    _, _, value, tenant_id = query.data.split(":", 3)
    if value == "date":
        await begin_flow(
            query,
            state,
            TenantAccessStates.date,
            "tenant_access",
            "Введите новую дату окончания в формате ГГГГ-ММ-ДД.",
        )
        await state.update_data(tenant_id=tenant_id)
        return
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).set_tenant_access(
            tenant_id, extend_days=int(value), active=True
        )
    await render(query, tenant_card(tenant), tenant_actions(tenant.id))


@router.message(TenantAccessStates.date)
async def tenant_access_date(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    try:
        value = parse_access_date(message.text or "")
        if value is None:
            raise ValueError("укажите конкретную дату")
    except ValueError as exc:
        await update_flow_screen(message, state, f"Ошибка: {escape(str(exc))}")
        return
    data = await state.get_data()
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).set_tenant_access(
            data["tenant_id"], expires_at=value, active=True
        )
    await state.clear()
    try:
        await message.bot.edit_message_text(
            chat_id=data["screen_chat_id"],
            message_id=data["screen_message_id"],
            text=tenant_card(tenant),
            reply_markup=tenant_actions(tenant.id),
        )
    except TelegramBadRequest:
        await message.answer(tenant_card(tenant), reply_markup=tenant_actions(tenant.id))


@router.callback_query(F.data.startswith("tenant:suspend:"))
@router.callback_query(F.data.startswith("tenant:resume:"))
async def tenant_access_status(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    active = query.data.startswith("tenant:resume:")
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        tenant = await service_for(session, settings, telegram_verifier).set_tenant_access(
            tenant_id, active=active
        )
    await render(query, tenant_card(tenant), tenant_actions(tenant.id, suspended=not active))


@router.callback_query(F.data.startswith("tenant:delete:"))
async def tenant_delete_prompt(query: CallbackQuery) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    await render(
        query,
        "<b>Удалить клиента?</b>\n\nБоты будут остановлены, данные скрыты из активного списка. Действие требует явного подтверждения.",
        delete_confirmation(tenant_id),
    )


@router.callback_query(F.data.startswith("tenant:delete_confirm:"))
async def tenant_delete_confirm(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        await service_for(session, settings, telegram_verifier).delete_tenant(tenant_id)
    await render(query, "Клиент удалён из активного списка.", back_to_owner_menu())


@router.callback_query(F.data.startswith("tenant:activity:"))
async def tenant_activity(
    query: CallbackQuery, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        total = await session.scalar(
            select(func.count(ProductEvent.id)).where(ProductEvent.tenant_id == tenant_id)
        )
        last = await session.scalar(
            select(ProductEvent)
            .where(ProductEvent.tenant_id == tenant_id)
            .order_by(ProductEvent.occurred_at.desc())
            .limit(1)
        )
    text_value = (
        f"<b>Активность клиента</b>\n\nСобытий: {total or 0}\n"
        f"Последнее: {escape(last.event_name) if last else '—'}"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")]
        ]
    )
    await render(query, text_value, markup)


@router.callback_query(F.data.startswith("tenant:bot:"))
async def tenant_bot(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        tenant = await service.get_tenant(tenant_id)
        bots = await service.list_bots(tenant_id)
    if not bots:
        await render(query, "Клиентский бот ещё не подключён.", tenant_bot_missing(tenant_id))
        return
    bot = bots[0]
    stats = await ProductEventService(session_factory).stats(bot.id, tenant_id)
    miniapp = sum(
        count for name, count in stats.popular_buttons if name == "miniapp_button_clicked"
    )
    await render(
        query,
        bot_card(bot, tenant.name, stats.unique_users, miniapp),
        bot_actions(bot.id, bot.username),
    )


@router.callback_query(F.data == "owner:bots")
async def bots_menu(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        tenants = await service.list_tenants()
        entries: list[tuple[str, str]] = []
        for tenant in tenants:
            for bot in await service.list_bots(tenant.id):
                entries.append((tenant.name, bot.id))
    if not entries:
        await render(query, "Клиентские боты пока не подключены.", back_to_owner_menu())
        return
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"bot:view:{bot_id}")]
        for name, bot_id in entries
    ]
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="owner:menu")])
    await render(
        query,
        "<b>Клиентские боты</b>\n\nВыберите проект.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("bot:view:"))
async def bot_view(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    bot_id = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        bot = await service.get_bot(bot_id)
        tenant = await service.get_tenant(bot.tenant_id)
    stats = await ProductEventService(session_factory).stats(bot.id, bot.tenant_id)
    miniapp = sum(
        count for name, count in stats.popular_buttons if name == "miniapp_button_clicked"
    )
    await render(
        query,
        bot_card(bot, tenant.name, stats.unique_users, miniapp),
        bot_actions(bot.id, bot.username),
    )


@router.callback_query(F.data.startswith("bot:create:"))
async def bot_create_start(query: CallbackQuery, state: FSMContext) -> None:
    tenant_id = query.data.rsplit(":", 1)[1]
    await begin_flow(
        query,
        state,
        BotCreateStates.token,
        "bot_create",
        "Отправьте новый BotFather token. Сообщение будет удалено сразу после чтения.",
    )
    await state.update_data(tenant_id=tenant_id)


@router.message(BotCreateStates.token)
async def bot_create_finish(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    token = message.text or ""
    data = await state.get_data()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - secret message deletion is best-effort
        logger.warning("Could not delete a BotFather token message")
    try:
        payload = BotCreate(token=token)
        async with session_factory() as session:
            service = service_for(session, settings, telegram_verifier)
            bot = await service.create_bot(data["tenant_id"], payload)
            tenant = await service.get_tenant(data["tenant_id"])
    except (ValidationError, BotTokenVerificationError, BotAlreadyExistsError) as exc:
        await update_flow_screen(
            message,
            state,
            f"Бот не создан: {escape(str(exc))}. Token не сохранён.\n\nОтправьте корректный token или отмените действие.",
        )
        return
    finally:
        token = ""
    await state.clear()
    stats = await ProductEventService(session_factory).stats(bot.id, bot.tenant_id)
    try:
        await message.bot.edit_message_text(
            chat_id=data["screen_chat_id"],
            message_id=data["screen_message_id"],
            text=bot_card(bot, tenant.name, stats.unique_users),
            reply_markup=bot_actions(bot.id, bot.username),
        )
    except TelegramBadRequest:
        await message.answer(
            bot_card(bot, tenant.name), reply_markup=bot_actions(bot.id, bot.username)
        )


@router.callback_query(F.data.startswith("bot:action:"))
async def bot_action(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    _, _, action, bot_id = query.data.split(":", 3)
    async with session_factory() as session:
        service = service_for(session, settings, telegram_verifier)
        bot = await service.get_bot(bot_id)
        tenant = await service.get_tenant(bot.tenant_id)
        if action == "start":
            bot = await service.set_bot_enabled(bot_id, True)
        elif action == "stop":
            bot = await service.set_bot_enabled(bot_id, False)
        elif action == "restart":
            bot = await service.restart_bot(bot_id)
        elif action == "check":
            try:
                await service.verify_bot(bot_id)
                await query.answer("Token действителен", show_alert=True)
            except (BotTokenVerificationError, RuntimeError):
                await query.answer("Token недействителен или отозван", show_alert=True)
            return
        elif action == "rotate":
            await begin_flow(
                query,
                state,
                BotRotateStates.token,
                "bot_rotate",
                "Отправьте новый BotFather token. Сообщение будет удалено.",
            )
            await state.update_data(bot_id=bot_id)
            return
        elif action == "stats":
            stats = await ProductEventService(session_factory).stats(bot.id, bot.tenant_id)
            popular = (
                ", ".join(f"{escape(name)}: {count}" for name, count in stats.popular_buttons)
                or "—"
            )
            await render(
                query,
                f"<b>Статистика @{escape(bot.username)}</b>\n\n"
                f"Пользователи: {stats.unique_users}\nСобытия: {stats.total_events}\n"
                f"За 24 часа: {stats.events_last_24h}\nПопулярные кнопки: {popular}",
                bot_actions(bot.id, bot.username),
            )
            return
    stats = await ProductEventService(session_factory).stats(bot.id, bot.tenant_id)
    await render(
        query, bot_card(bot, tenant.name, stats.unique_users), bot_actions(bot.id, bot.username)
    )


@router.message(BotRotateStates.token)
async def bot_rotate_finish(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_verifier: TelegramBotVerifier,
) -> None:
    token = message.text or ""
    data = await state.get_data()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - secret message deletion is best-effort
        logger.warning("Could not delete a BotFather token rotation message")
    try:
        async with session_factory() as session:
            bot = await service_for(session, settings, telegram_verifier).rotate_bot_token(
                data["bot_id"], token
            )
    except (ValidationError, BotTokenVerificationError, RuntimeError) as exc:
        await update_flow_screen(message, state, f"Token не заменён: {escape(str(exc))}")
        return
    finally:
        token = ""
    await state.clear()
    try:
        await message.bot.edit_message_text(
            chat_id=data["screen_chat_id"],
            message_id=data["screen_message_id"],
            text=f"Token заменён. Runtime @{escape(bot.username)} будет перезапущен.",
            reply_markup=bot_actions(bot.id, bot.username),
        )
    except TelegramBadRequest:
        await message.answer("Token заменён.", reply_markup=owner_main_menu())


@router.callback_query(F.data == "owner:activity")
async def owner_activity(
    query: CallbackQuery, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        clients = int(await session.scalar(select(func.count(Tenant.id))) or 0)
        connections = int(
            await session.scalar(
                select(func.count(TelegramConnection.id)).where(
                    TelegramConnection.status.in_(("connected", "ready", "syncing")),
                    TelegramConnection.deleted_at.is_(None),
                )
            )
            or 0
        )
        employees = int(await session.scalar(select(func.count(Employee.id))) or 0)
        dialogs = int(
            await session.scalar(
                select(func.count(TelegramDialog.id)).where(TelegramDialog.selected.is_(True))
            )
            or 0
        )
        groups = int(await session.scalar(select(func.count(GroupIntegration.id))) or 0)
        messages = int(await session.scalar(select(func.count(TelegramMessage.id))) or 0)
        queue_size = int(
            await session.scalar(
                select(func.count(BackgroundJob.id)).where(
                    BackgroundJob.status.in_(
                        ("pending", "scheduled", "waiting", "retry_scheduled", "running")
                    )
                )
            )
            or 0
        )
        fast_calls = int(
            await session.scalar(
                select(func.count(AIUsageCall.id)).where(AIUsageCall.job_type == "signal.ai_triage")
            )
            or 0
        )
        deep_calls = int(
            await session.scalar(
                select(func.count(AIUsageCall.id)).where(
                    AIUsageCall.job_type == "ai_batch_analysis"
                )
            )
            or 0
        )
        notifications = int(await session.scalar(select(func.count(NotificationLog.id))) or 0)
        notification_errors = int(
            await session.scalar(
                select(func.count(NotificationLog.id)).where(NotificationLog.status == "failed")
            )
            or 0
        )
        tokens, cost = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(AIUsageCall.input_tokens + AIUsageCall.output_tokens), 0
                    ),
                    func.coalesce(func.sum(AIUsageCall.estimated_cost), 0.0),
                )
            )
        ).one()
        reconnects = int(
            await session.scalar(
                select(func.count(ProductEvent.id)).where(
                    ProductEvent.event_name.in_(
                        ("telegram_reconnected", "telegram_connection_completed")
                    )
                )
            )
            or 0
        )
    await render(
        query,
        "<b>Статистика Ventrix</b>\n\n"
        f"Клиенты: <b>{clients}</b>\n"
        f"Активные Telegram connections: <b>{connections}</b>\n"
        f"Сотрудники: <b>{employees}</b>\n"
        f"Отслеживаемые диалоги: <b>{dialogs}</b>\n"
        f"Рабочие группы: <b>{groups}</b>\n"
        f"Сообщения обработаны: <b>{messages}</b>\n"
        f"Очередь: <b>{queue_size}</b>\n\n"
        f"AI fast calls: <b>{fast_calls}</b>\n"
        f"AI deep calls: <b>{deep_calls}</b>\n"
        f"Tokens: <b>{int(tokens or 0)}</b>\n"
        f"Оценочная стоимость: <b>{float(cost or 0):.4f}</b>\n\n"
        f"Уведомления: <b>{notifications}</b>\n"
        f"Ошибки доставки: <b>{notification_errors}</b>\n"
        f"Reconnects: <b>{reconnects}</b>",
        back_to_owner_menu(),
    )


@router.callback_query(F.data == "owner:system")
async def system_status(
    query: CallbackQuery,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db_status = "ошибка"
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        db_status = "готова"
    except Exception:  # noqa: BLE001 - status screen degrades safely
        logger.warning("Owner status database probe failed")
    me = await bot.get_me()
    await render(
        query,
        f"<b>Состояние системы</b>\n\nSQLite: {db_status}\n"
        f"Административный бот: @{escape(me.username or '—')}\nPolling: активен",
        back_to_owner_menu(),
    )


def system_secret_label(name: str) -> str:
    return {
        "telegram_api_id": "Telegram API ID",
        "telegram_api_hash": "Telegram API Hash",
        "deepseek_api_key": "DeepSeek API key",
    }[name]


def configured_secret(settings: Settings, name: str) -> str | None:
    if name == "telegram_api_id":
        return str(settings.telegram_api_id) if settings.telegram_api_id else None
    value = settings.telegram_api_hash if name == "telegram_api_hash" else settings.deepseek_api_key
    return value.get_secret_value() if value else None


@router.callback_query(F.data == "owner:settings")
async def owner_settings(query: CallbackQuery) -> None:
    await render(
        query,
        "<b>Настройки системы</b>\n\n"
        "Секреты показываются в маскированном виде. Новое значение применяется после "
        "явного подтверждения и перезапуска сервисов Ventrix.",
        system_settings_menu(),
    )


@router.callback_query(F.data.startswith("owner:secret:"))
async def owner_secret_view(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    name = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        service = SystemSecretService(
            session, EncryptionService(settings.app_encryption_key.get_secret_value())
        )
        value = await service.get(name) or configured_secret(settings, name)
    await render(
        query,
        f"<b>{system_secret_label(name)}</b>\n\nТекущее значение: <code>{escape(mask_secret(value))}</code>",
        system_secret_actions(name),
    )


@router.callback_query(F.data.startswith("owner:secret_reveal:"))
async def owner_secret_reveal(
    query: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    name = query.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        service = SystemSecretService(
            session, EncryptionService(settings.app_encryption_key.get_secret_value())
        )
        value = await service.get(name) or configured_secret(settings, name)
    await query.answer(value or "Значение не настроено", show_alert=True)


@router.callback_query(F.data.startswith("owner:secret_change:"))
async def owner_secret_change(query: CallbackQuery, state: FSMContext) -> None:
    name = query.data.rsplit(":", 1)[1]
    await begin_flow(
        query,
        state,
        SystemSecretStates.value,
        "system_secret",
        f"<b>Изменить {system_secret_label(name)}</b>\n\n"
        "Отправьте новое значение. Сообщение будет удалено сразу после обработки.",
    )
    await state.update_data(secret_name=name)


@router.message(SystemSecretStates.value)
async def owner_secret_value(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    value = message.text or ""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    try:
        async with session_factory() as session:
            service = SystemSecretService(
                session, EncryptionService(settings.app_encryption_key.get_secret_value())
            )
            staged = await service.stage(str(data["secret_name"]), value)
            masked = mask_secret(value.strip())
    except ValueError as exc:
        value = ""
        await update_flow_screen(message, state, f"Значение не принято: {escape(str(exc))}")
        return
    finally:
        value = ""
    await state.update_data(staged_secret_id=staged.id)
    await state.set_state(SystemSecretStates.confirm)
    await update_flow_screen(
        message,
        state,
        f"Заменить {system_secret_label(str(data['secret_name']))}?\n\n"
        f"Новое значение: <code>{escape(masked)}</code>",
        system_secret_confirmation(str(data["secret_name"])),
    )


@router.callback_query(SystemSecretStates.confirm, F.data.startswith("owner:secret_confirm:"))
async def owner_secret_confirm(
    query: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    name = query.data.rsplit(":", 1)[1]
    data = await state.get_data()
    async with session_factory() as session:
        await SystemSecretService(
            session, EncryptionService(settings.app_encryption_key.get_secret_value())
        ).confirm(name, str(data["staged_secret_id"]))
    await state.clear()
    await render(
        query,
        f"{system_secret_label(name)} сохранён в зашифрованном виде.\n\n"
        "Значение будет использовано после перезапуска сервисов Ventrix.",
        system_settings_menu(),
    )


@router.message()
async def unmatched_text(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(
            "Используйте кнопки меню или команду /menu.", reply_markup=owner_main_menu()
        )
