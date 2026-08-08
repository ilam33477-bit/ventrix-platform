from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject


class ActiveFlowGuardMiddleware(BaseMiddleware):
    """Prevents unrelated inline actions from crossing an active FSM flow."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        state: FSMContext | None = data.get("state")
        active_state = await state.get_state() if state else None
        callback = event.data or ""
        allowed = callback.startswith("flow:") or callback == "tenant:edit_confirm"
        if active_state and not allowed:
            await event.answer("Сначала завершите или отмените текущее действие.", show_alert=True)
            return None
        return await handler(event, data)
