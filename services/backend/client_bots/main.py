from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from ..config import get_settings
from ..database import get_session_factory
from ..services.encryption import EncryptionService
from ..services.product_events import ProductEventService
from ..services.system_secrets import load_runtime_secret_overrides
from ..telegram_sessions.gateway import TelethonGateway
from ..telegram_sessions.service import TelegramConnectionService
from .runtime import AiogramPollingRuntime, ClientBotRuntimeManager


async def run() -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    settings = await load_runtime_secret_overrides(session_factory, settings)
    logging.basicConfig(level=settings.log_level)
    # Telegram embeds bot tokens in request URLs. Never allow HTTP client URL logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    events = ProductEventService(session_factory)
    encryption = EncryptionService(settings.app_encryption_key.get_secret_value())
    connection_service = None
    if settings.telegram_api_id and settings.telegram_api_hash:
        connection_service = TelegramConnectionService(
            session_factory,
            encryption,
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

    def factory(token: str, tenant_id: str, bot_instance_id: str) -> AiogramPollingRuntime:
        return AiogramPollingRuntime(
            token,
            tenant_id,
            bot_instance_id,
            session_factory,
            events,
            mini_app_url=settings.client_mini_app_url,
            fsm_ttl=timedelta(hours=settings.fsm_ttl_hours),
            connection_service=connection_service,
        )

    manager = ClientBotRuntimeManager(
        session_factory,
        encryption,
        factory,
        sync_interval_seconds=settings.client_bot_sync_interval_seconds,
        heartbeat_seconds=settings.client_bot_heartbeat_seconds,
        restart_backoff_seconds=settings.client_bot_restart_backoff_seconds,
    )
    try:
        await manager.run()
    finally:
        await manager.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
