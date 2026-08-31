from __future__ import annotations

from typing import Any

from aiogram.types import MenuButtonWebApp, WebAppInfo


async def ensure_mini_app_menu_button(bot: Any, mini_app_url: str) -> bool:
    """Ensure the tenant bot exposes the production Mini App menu button."""
    current = await bot.get_chat_menu_button()
    current_url = getattr(getattr(current, "web_app", None), "url", None)
    current_text = getattr(current, "text", None)
    if (
        current_url
        and current_url.rstrip("/") == mini_app_url.rstrip("/")
        and current_text == "Ventrix AI"
    ):
        return False
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Ventrix AI",
            web_app=WebAppInfo(url=mini_app_url),
        )
    )
    return True
