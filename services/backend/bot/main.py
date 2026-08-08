from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation

from ..config import get_settings
from ..database import get_session_factory
from ..services.telegram import TelegramBotVerifier
from .auth import OwnerOnlyMiddleware
from .flow_guard import ActiveFlowGuardMiddleware
from .handlers import router
from .sqlite_storage import SQLiteFSMStorage


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Telegram embeds bot tokens in request URLs. Never allow HTTP client URL logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bot = Bot(
        settings.telegram_owner_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    session_factory = get_session_factory()
    storage = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=settings.fsm_ttl_hours))
    await storage.cleanup_expired()
    dispatcher = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.platform_owner_telegram_id))
    dispatcher.callback_query.outer_middleware(ActiveFlowGuardMiddleware())
    dispatcher.include_router(router)
    await dispatcher.start_polling(
        bot,
        settings=settings,
        session_factory=session_factory,
        telegram_verifier=TelegramBotVerifier(settings.telegram_api_base_url),
        allowed_updates=dispatcher.resolve_used_update_types(),
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
