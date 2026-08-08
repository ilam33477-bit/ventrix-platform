from __future__ import annotations

import asyncio
import logging

from ..config import get_settings
from ..database import get_session_factory
from ..jobs.queue import SQLiteJobQueue
from .service import TenantAnalysisScheduler

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    session_factory = get_session_factory()
    queue = SQLiteJobQueue(
        session_factory,
        max_active_tenant_jobs=settings.max_active_tenant_jobs,
        tenant_max_active_heavy_jobs=settings.tenant_max_active_heavy_jobs,
        category_limits={
            "ai": settings.max_active_ai_requests,
            "ai_fast": settings.max_active_ai_fast_requests,
            "ai_heavy": settings.max_active_ai_heavy_requests,
            "telegram": settings.max_active_telegram_requests,
            "sync": settings.max_active_sync_jobs,
            "report": settings.max_active_report_jobs,
        },
    )
    scheduler = TenantAnalysisScheduler(
        session_factory,
        queue,
        incremental_interval_seconds=int(settings.incremental_sync_interval_seconds),
        reconciliation_interval_seconds=settings.hourly_reconciliation_interval_seconds,
    )
    while True:
        try:
            await scheduler.tick()
        except Exception:
            logger.exception("Scheduler tick failed; continuing after poll interval")
        await asyncio.sleep(settings.scheduler_poll_interval_seconds)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
