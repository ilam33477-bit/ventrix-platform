from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


def is_platform_owner(user_id: int | None, configured_owner_id: int) -> bool:
    return user_id is not None and user_id == configured_owner_id


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if is_platform_owner(getattr(user, "id", None), self.owner_id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("Доступ запрещён. Этот бот доступен только владельцу платформы.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён", show_alert=True)
        return None
