from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from services.api.deepseek import DeepSeekProvider
from services.backend.analysis.budget import ModelInputBudget

from ..analysis.service import AnalysisPipelineService
from ..bot.sqlite_storage import SQLiteFSMStorage
from ..config import get_settings
from ..database import get_session_factory
from ..intelligence.ai_triage import AITriageService
from ..intelligence.feedback_learning import TenantFeedbackLearningService
from ..intelligence.notifications import (
    NotificationDispatcher,
    TelegramBotNotificationSender,
)
from ..intelligence.reconciliation import ReconciliationService
from ..intelligence.signals import SignalService
from ..observability import configure_structured_logging, log_event
from ..services.encryption import EncryptionService
from ..services.system_secrets import load_runtime_secret_overrides
from ..telegram_sessions.event_ingestion import TelegramEventIngestion
from ..telegram_sessions.gateway import TelethonGateway
from ..telegram_sessions.service import TelegramConnectionService
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
        allowed_categories: frozenset[str] | None = None,
        telegram_account_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = handlers
        self.heartbeat_seconds = heartbeat_seconds
        self.allowed_categories = allowed_categories
        self.telegram_account_id = telegram_account_id

    async def _heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.queue.heartbeat(lease):
                return

    async def run_once(self) -> bool:
        lease = await self.queue.claim_next(
            self.worker_id,
            allowed_categories=self.allowed_categories,
            telegram_account_id=self.telegram_account_id,
        )
        if lease is None:
            return False
        handler = self.handlers.get(lease.job_type)
        if handler is None:
            await self.queue.fail(lease, LookupError("unsupported job type"))
            return True
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "background_job_started",
            job_id=lease.id,
            tenant_id=lease.tenant_id,
            account_id=lease.telegram_account_id,
            dialog_id=lease.dialog_id,
            correlation_id=lease.correlation_id,
            stage=lease.job_type,
            category=lease.category,
            worker_id=self.worker_id,
        )
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
            log_event(
                logger,
                logging.INFO,
                "background_job_completed",
                job_id=lease.id,
                tenant_id=lease.tenant_id,
                account_id=lease.telegram_account_id,
                dialog_id=lease.dialog_id,
                correlation_id=lease.correlation_id,
                stage=lease.job_type,
                category=lease.category,
                duration_ms=int((time.perf_counter() - started) * 1000),
                worker_id=self.worker_id,
            )
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
    settings = await load_runtime_secret_overrides(session_factory, settings)
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
            "notification": settings.max_active_notification_jobs,
        },
    )
    worker_id = f"{settings.worker_id}:{socket.gethostname()}:{os.getpid()}"
    handlers = dict(HANDLERS)
    connection_service = None
    encryption = EncryptionService(settings.app_encryption_key.get_secret_value())
    if settings.telegram_api_id and settings.telegram_api_hash:
        gateway = TelethonGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            device_model=settings.telegram_device_model,
            system_version=settings.telegram_system_version,
            app_version=settings.telegram_app_version,
            lang_code=settings.telegram_lang_code,
            system_lang_code=settings.telegram_system_lang_code,
        )
        connection_service = TelegramConnectionService(session_factory, encryption, gateway)
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
        model_budget=ModelInputBudget(
            context_window=settings.deepseek_context_window_tokens,
            max_output_tokens=settings.deepseek_max_output_tokens,
            safety_margin_tokens=settings.deepseek_safety_margin_tokens,
            max_dialogs_per_request=settings.deepseek_max_dialogs_per_request,
            overlap_tokens=settings.deepseek_dialog_overlap_tokens,
        ),
    )
    maintenance = MaintenanceJobHandlers(
        session_factory,
        analysis,
        connection_service=connection_service,
        fsm_ttl_hours=settings.fsm_ttl_hours,
    )
    signals = SignalService(session_factory, queue)
    event_ingestion = TelegramEventIngestion(session_factory, queue)
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
        verification_provider=provider,
        verification_model=settings.deepseek_fast_model,
    )
    feedback_learning = TenantFeedbackLearningService(
        session_factory,
        provider,
        model=settings.deepseek_fast_model,
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
            "analysis.aggregate": analysis.aggregate_tenant_run,
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
            "signal.scan_batch": signals.scan_batch_job,
            "telegram.ingest_event": event_ingestion.ingest,
            "signal.ai_triage": triage.triage,
            "commitment.reconcile": reconciliation.reconcile,
            "commitment.deadline_check": reconciliation.deadline_check,
            "dialog.sla_check": reconciliation.sla_check,
            "problem.evaluate": reconciliation.evaluate_problem,
            "analysis.hourly": reconciliation.reconcile,
            "feedback.synthesize": feedback_learning.synthesize,
            "notification.employee": notification_dispatcher.dispatch,
            "notification.manager": notification_dispatcher.dispatch,
            "notification.group": notification_dispatcher.dispatch,
            "notification.initial_summary": notification_dispatcher.initial_summary,
            "maintenance.session_health": maintenance.session_health_check,
        }
    )
    lock_timeout = timedelta(seconds=settings.worker_lock_timeout_seconds)
    await queue.recover_stale(lock_timeout)
    storage = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=settings.fsm_ttl_hours))

    pool_specs = (
        (
            "realtime",
            frozenset({"realtime", "critical"}),
            settings.worker_realtime_concurrency,
        ),
        (
            "notification",
            frozenset({"notification"}),
            settings.worker_notification_concurrency,
        ),
        (
            "telegram",
            frozenset({"telegram", "sync", "historical"}),
            settings.worker_telegram_concurrency,
        ),
        ("ai", frozenset({"ai", "ai_fast"}), settings.worker_ai_concurrency),
        (
            "heavy",
            frozenset({"ai_heavy", "analysis", "report"}),
            settings.worker_heavy_concurrency,
        ),
        (
            "general",
            frozenset({"general", "reconciliation"}),
            settings.worker_general_concurrency,
        ),
    )

    async def worker_loop(worker: BackgroundWorker) -> None:
        idle_delay = settings.worker_poll_interval_seconds
        max_idle_delay = max(idle_delay, min(5.0, idle_delay * 4))
        while True:
            if await worker.run_once():
                idle_delay = settings.worker_poll_interval_seconds
                # Give SQLite and the event loop a scheduling point between jobs.
                # This prevents a large historical backlog from monopolising a
                # single-core host while preserving the configured concurrency.
                await asyncio.sleep(0)
                continue
            await asyncio.sleep(idle_delay)
            idle_delay = min(max_idle_delay, idle_delay * 2)

    async def maintenance_loop() -> None:
        while True:
            await asyncio.sleep(min(60.0, float(settings.worker_lock_timeout_seconds) / 2))
            await storage.cleanup_expired()
            await queue.recover_stale(lock_timeout)

    log_event(
        logger,
        logging.INFO,
        "background_worker_pools_started",
        worker_id=worker_id,
        pools={name: concurrency for name, _, concurrency in pool_specs},
    )
    async with asyncio.TaskGroup() as tasks:
        for pool_name, categories, concurrency in pool_specs:
            for index in range(concurrency):
                tasks.create_task(
                    worker_loop(
                        BackgroundWorker(
                            queue,
                            f"{worker_id}:{pool_name}:{index + 1}",
                            handlers,
                            heartbeat_seconds=settings.worker_heartbeat_seconds,
                            allowed_categories=categories,
                        )
                    ),
                    name=f"worker-{pool_name}-{index + 1}",
                )
        tasks.create_task(maintenance_loop(), name="worker-maintenance")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
