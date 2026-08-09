from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text

from ..config import get_settings
from ..database import get_session_factory
from ..metrics import collect_runtime_metrics
from ..models import BackgroundJob, BotInstance, RuntimeHealth, TelegramConnection
from ..services.encryption import EncryptionService
from .client_router import router as client_router
from .router import router as owner_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Migrations are applied by the container entrypoint. Startup remains side-effect free.
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Опера Foundation API",
        version="0.2.0",
        docs_url="/api/docs" if os.getenv("ENVIRONMENT", "development") != "production" else None,
        lifespan=lifespan,
    )
    mini_app_url = get_settings().client_mini_app_url
    if mini_app_url:
        parsed = urlsplit(mini_app_url)
        mini_app_origin = f"{parsed.scheme}://{parsed.netloc}"
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[mini_app_origin],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live", tags=["system"])
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/ready", tags=["system"])
    async def ready(response: Response) -> dict[str, str]:
        try:
            async with get_session_factory()() as session:
                await session.execute(text("SELECT 1 FROM platform_owner LIMIT 1"))
            settings = get_settings()
            EncryptionService(settings.app_encryption_key.get_secret_value())
            return {"status": "ready"}
        except Exception:  # noqa: BLE001 - readiness must convert dependency failures to 503
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}

    @app.get("/health/ready", tags=["system"])
    async def health_ready(response: Response) -> dict[str, str]:
        return await ready(response)

    @app.get("/metrics", tags=["system"])
    async def metrics() -> dict[str, object]:
        async with get_session_factory()() as session:
            return await collect_runtime_metrics(session)

    @app.get("/health/details", tags=["system"])
    async def health_details() -> dict[str, object]:
        settings = get_settings()
        async with get_session_factory()() as session:
            queue_pending = int(
                await session.scalar(
                    select(func.count(BackgroundJob.id)).where(
                        BackgroundJob.status.in_(
                            ("pending", "scheduled", "waiting", "retry", "running")
                        )
                    )
                )
            )
            failed_jobs = int(
                await session.scalar(
                    select(func.count(BackgroundJob.id)).where(BackgroundJob.status == "failed")
                )
            )
            scheduler = await session.scalar(
                select(RuntimeHealth).where(RuntimeHealth.component == "scheduler")
            )
            owner_bots = int(
                await session.scalar(
                    select(func.count(BotInstance.id)).where(
                        BotInstance.runtime_status == "running"
                    )
                )
            )
            sessions = dict(
                (
                    await session.execute(
                        select(TelegramConnection.health_status, func.count(TelegramConnection.id))
                        .where(TelegramConnection.deleted_at.is_(None))
                        .group_by(TelegramConnection.health_status)
                    )
                ).all()
            )
        return {
            "status": "healthy",
            "database": "healthy",
            "queue": {"pending_or_running": queue_pending, "failed": failed_jobs},
            "scheduler": {
                "status": scheduler.status if scheduler else "not_started",
                "heartbeat_at": scheduler.heartbeat_at if scheduler else None,
            },
            "worker": "configured",
            "owner_bot": "configured",
            "client_runtime_manager": {"running_bots": owner_bots},
            "telethon_connections": sessions,
            "ai_provider": "configured" if settings.deepseek_api_key else "not_configured",
            "mini_app_api": "healthy",
        }

    app.include_router(owner_router)
    app.include_router(client_router)
    return app


app = create_app()
