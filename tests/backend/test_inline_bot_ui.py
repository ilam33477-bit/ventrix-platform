from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, User
from sqlalchemy import select

from services.backend.bot import handlers
from services.backend.bot.flow_guard import ActiveFlowGuardMiddleware
from services.backend.bot.keyboards import (
    client_main_menu,
    client_welcome_menu,
    owner_main_menu,
)
from services.backend.bot.sqlite_storage import SQLiteFSMStorage
from services.backend.bot.states import TenantCreateStates
from services.backend.client_bots.handlers import _access_status, build_client_router
from services.backend.models import Tenant


def button_texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_owner_and_client_navigation_is_inline_only() -> None:
    owner = owner_main_menu()
    client = client_main_menu()
    welcome = client_welcome_menu()
    assert isinstance(owner, InlineKeyboardMarkup)
    assert isinstance(client, InlineKeyboardMarkup)
    assert isinstance(welcome, InlineKeyboardMarkup)
    assert "👥 Клиенты" in button_texts(owner)
    assert "⚠️ Важное" in button_texts(client)
    assert button_texts(welcome) == ["Ventrix AI", "Главное меню"]


def test_client_interface_hides_platform_owner_support_and_commercial_terms() -> None:
    source = inspect.getsource(build_client_router).lower()
    for forbidden in ("platform_owner", "поддержка", "тариф", "цена", "deepseek"):
        assert forbidden not in source
    all_buttons = " ".join(
        button_texts(client_main_menu()) + button_texts(client_welcome_menu())
    ).lower()
    assert "поддерж" not in all_buttons
    assert "тариф" not in all_buttons


@pytest.mark.parametrize("raw", ["завтра вечером", "очень длинный текст вместо даты", "2026-99-40"])
def test_long_or_invalid_text_cannot_become_access_date(raw: str) -> None:
    with pytest.raises(ValueError):
        handlers.parse_access_date(raw)


def test_username_normalization_and_user_id_validation() -> None:
    assert handlers.normalize_username(" @Example_User ") == "example_user"
    assert handlers.normalize_username("пропустить") is None
    assert handlers.parse_user_id("835691584") == 835691584
    for invalid in ("@user", "12.5", "-1", "текст"):
        with pytest.raises(ValueError):
            handlers.parse_user_id(invalid)


@pytest.mark.asyncio
async def test_active_flow_blocks_unrelated_inline_scenario(monkeypatch) -> None:
    state = AsyncMock()
    state.get_state.return_value = "TenantCreateStates:name"
    event = CallbackQuery(
        id="callback-1",
        from_user=User(id=1, is_bot=False, first_name="Owner"),
        chat_instance="chat-1",
        data="owner:clients",
    )
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    middleware = ActiveFlowGuardMiddleware()
    handler = AsyncMock()
    result = await middleware(handler, event, {"state": state})
    assert result is None
    handler.assert_not_awaited()
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_clears_all_temporary_flow_data(monkeypatch) -> None:
    state = AsyncMock()
    query = AsyncMock()
    rendered = AsyncMock()
    monkeypatch.setattr(handlers, "render", rendered)
    await handlers.cancel_callback(query, state)
    state.clear.assert_awaited_once()
    rendered.assert_awaited_once()


def test_access_status_uses_only_access_mechanics(tenant_payload) -> None:
    tenant = type(
        "TenantView",
        (),
        {"status": "suspended", "subscription_expires_at": tenant_payload.subscription_expires_at},
    )()
    text = _access_status(tenant).lower()
    assert "приостановлен" in text
    assert "тариф" not in text and "цена" not in text


@pytest.mark.asyncio
async def test_owner_fsm_access_date_completes_tenant_creation(
    session_factory, settings, verifier, monkeypatch
) -> None:
    storage = SQLiteFSMStorage(session_factory)
    key = StorageKey(bot_id=100, chat_id=200, user_id=300)
    state = FSMContext(storage=storage, key=key)
    expires_at = datetime.now(UTC).date() + timedelta(days=30)
    await state.set_state(TenantCreateStates.access_end)
    await state.set_data(
        {
            "name": "FSM Client",
            "owner_telegram_username": "fsm_owner",
            "owner_telegram_user_id": 900001,
            "niche": "B2B services",
            "target_audience": "Operations directors",
            "additional_ai_instructions": "Check unanswered client requests",
        }
    )
    monkeypatch.setattr(handlers, "update_flow_screen", AsyncMock())
    monkeypatch.setattr(handlers, "render", AsyncMock())
    message = AsyncMock()
    message.text = expires_at.isoformat()

    await handlers.access_end(message, state)

    saved_data = await state.get_data()
    assert saved_data["subscription_expires_at"] == expires_at.isoformat()
    await handlers.tenant_create_confirm(AsyncMock(), state, session_factory, settings, verifier)

    async with session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.name == "FSM Client"))
    assert tenant is not None
    assert tenant.subscription_expires_at == expires_at
    assert await state.get_data() == {}
