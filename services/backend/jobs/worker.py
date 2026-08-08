from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from services.api.deepseek import DeepSeekProvider

from ..analysis.service import AnalysisPipelineService
from ..bot.sqlite_storage import SQLiteFSMStorage
from ..config import get_settings
from ..database import get_session_factory
from ..intelligence.ai_triage import AITriageService
from ..intelligence.notifications import (
    NotificationDispatcher,
    TelegramBotNotificationSender,
)
from ..intelligence.reconciliation import ReconciliationService
from ..intelligence.signals import SignalService
from ..observability import configure_structured_logging, log_event
from ..services.encryption import EncryptionService
from ..telegram_sessions.gateway import TelethonGateway
from ..telegram_sessions.incremental import IncrementalTelegramIngestion
from ..telegram_sessions.service import TelegramConnectionService
from ..telegram_sessions.sync import TelegramSyncHandlers
from .maintenance import MaintenanceJobHandlers
from .queue import JobDeferred, JobLease, SQLiteJobQueue

logger = logging.getLogger(__name__)
JobHandler = Callable[[JobLease], Awaitable[dict[str, Any]]]


async def echo_handler(job: JobLease) -> dict[str, Any]:
    return {"echo": job.payload, "attempt": job.attempts + 1}


async def fail_once_handler(job: JobLease) -> dict[str, Any]:
    if job.attempts == 0:
        raise RuntimeError("intentional first-attempt failure")
    return {"recovered": True, "attempt": job.attempts + 1}


HANDLERS: dict[str, JobHandler] = {
    "system.echo": echo_handler,
    "system.fail_once": fail_once_handler,
}


class BackgroundWorker:
    def __init__(
        self,
        queue: SQLiteJobQueue,
        worker_id: str,
        handlers: dict[str, JobHandler],
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = handlers
        self.heartbeat_seconds = heartbeat_seconds

    async def _heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.queue.heartbeat(lease):
                return

    async def run_once(self) -> bool:
        lease = await self.queue.claim_next(self.worker_id)
        if lease is None:
            return False
        handler = self.handlers.get(lease.job_type)
        if handler is None:
            await self.queue.fail(lease, LookupError("unsupported job type"))
            return True
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            result = await handler(lease)
        except JobDeferred as exc:
            await self.queue.defer(lease, exc.delay_seconds, exc.reason)
        except Exception as exc:  # noqa: BLE001 - job failures are persisted and retried
            status = await self.queue.fail(lease, exc)
            log_event(
                logger,
                logging.WARNING,
                "background_job_failed",
                job_id=lease.id,
                tenant_id=lease.tenant_id,
                retry_count=lease.attempts + 1,
                status=status,
                error_type=type(exc).__name__,
            )
        else:
            await self.queue.complete(lease, result)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True


async def run() -> None:
    settings = get_settings()
    configure_structured_logging(settings.log_level)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
    worker_id = f"{settings.worker_id}:{socket.gethostname()}:{os.getpid()}"
    handlers = dict(HANDLERS)
    connection_service = None
    incremental = None
    encryption = EncryptionService(settings.app_encryption_key.get_secret_value())
    if settings.telegram_api_id and settings.telegram_api_hash:
        gateway = TelethonGateway(
            settings.telegram_api_id, settings.telegram_api_hash.get_secret_value()
        )
        telegram_handlers = TelegramSyncHandlers(
            session_factory,
            encryption,
            gateway,
            batch_size=settings.telegram_sync_batch_size,
            batch_pause_seconds=settings.telegram_sync_batch_pause_seconds,
            max_messages_per_chat=settings.telegram_sync_max_messages_per_chat,
        )
        handlers["telegram.sync_chat"] = telegram_handlers.sync_chat
        connection_service = TelegramConnectionService(session_factory, encryption, gateway)
        incremental = IncrementalTelegramIngestion(
            session_factory,
            encryption,
            gateway,
            queue,
            batch_size=settings.telegram_sync_batch_size,
        )
    provider = None
    if settings.deepseek_api_key:
        provider = DeepSeekProvider(
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.ai_request_timeout_seconds,
            api_key_value=settings.deepseek_api_key.get_secret_value(),
        )
    analysis = AnalysisPipelineService(
        session_factory,
        encryption,
        connection_service=connection_service,
        provider=provider,
        queue=queue,
        token_budget=settings.tenant_report_token_budget,
        fast_model=settings.deepseek_fast_model,
        deep_model=settings.deepseek_deep_model,
    )
    maintenance = MaintenanceJobHandlers(
        session_factory,
        analysis,
        connection_service=connection_service,
        fsm_ttl_hours=settings.fsm_ttl_hours,
    )
    signals = SignalService(session_factory, queue)
    triage = AITriageService(
        session_factory,
        queue,
        provider,
        model=settings.deepseek_fast_model,
        context_limit=settings.signal_context_message_limit,
        notification_cooldown_minutes=settings.notification_cooldown_minutes,
    )
    reconciliation = ReconciliationService(
        session_factory,
        queue,
        notification_cooldown_minutes=settings.notification_cooldown_minutes,
    )
    notification_sender = TelegramBotNotificationSender(
        session_factory,
        encryption,
        settings.telegram_api_base_url,
    )
    notification_dispatcher = NotificationDispatcher(session_factory, notification_sender)
    handlers.update(
        {
            "analysis.pipeline": analysis.pipeline,
            "analysis.connection": analysis.pipeline,
            "analysis.deep": analysis.pipeline,
            "telegram_initial_sync": maintenance.telegram_sync,
            "telegram_incremental_sync": maintenance.telegram_sync,
            "dialog_classification": maintenance.dialog_classification,
            "message_preprocessing": maintenance.message_preprocessing,
            "ai_batch_analysis": analysis.process_batch,
            "problem_deduplication": maintenance.problem_deduplication,
            "report_generation": analysis.generate_report,
            "report.employee": analysis.generate_report,
            "report.client": analysis.generate_report,
            "report.company": analysis.generate_report,
            "report_delivery": maintenance.report_delivery,
            "statistics_refresh": maintenance.statistics_refresh,
            "session_health_check": maintenance.session_health_check,
            "cleanup": maintenance.cleanup,
            "retry_failed_job": maintenance.retry_failed_job,
            "signal.local_scan": signals.local_scan_job,
            "signal.ai_triage": triage.triage,
            "commitment.reconcile": reconciliation.reconcile,
            "problem.evaluate": reconciliation.evaluate_problem,
            "analysis.hourly": reconciliation.reconcile,
            "notification.employee": notification_dispatcher.dispatch,
            "notification.manager": notification_dispatcher.dispatch,
            "notification.group": notification_dispatcher.dispatch,
            "maintenance.session_health": maintenance.session_health_check,
        }
    )
    if incremental is not None:
        handlers["telegram.fetch_updates"] = incremental.fetch_updates
        handlers["telegram.history_sync"] = maintenance.telegram_sync
    worker = BackgroundWorker(
        queue, worker_id, handlers, heartbeat_seconds=settings.worker_heartbeat_seconds
    )
    lock_timeout = timedelta(seconds=settings.worker_lock_timeout_seconds)
    await queue.recover_stale(lock_timeout)
    storage = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=settings.fsm_ttl_hours))
    cleanup_counter = 0
    while True:
        worked = await worker.run_once()
        cleanup_counter += 1
        if cleanup_counter >= 300:
            await storage.cleanup_expired()
            await queue.recover_stale(lock_timeout)
            cleanup_counter = 0
        if not worked:
            await asyncio.sleep(settings.worker_poll_interval_seconds)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
