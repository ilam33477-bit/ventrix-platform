from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..jobs.queue import JobDeferred, JobLease, SQLiteJobQueue
from ..metrics import collect_runtime_metrics
from ..models import (
    AIUsageCall,
    BackgroundJob,
    NotificationLog,
    ReportGenerationRun,
    RuntimeHealth,
)
from ..observability import redact_log_text

MAX_EXPORT_ROWS = 5000
MAX_EXPORT_BYTES = 1_500_000
SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_code(value: object) -> str | None:
    if value is None:
        return None
    return SAFE_CODE_RE.sub("_", redact_log_text(value))[:100]


async def build_operational_log_export(
    session: AsyncSession,
    *,
    hours: int,
    now: datetime | None = None,
) -> bytes:
    """Build a bounded, allowlisted JSONL export without payloads or message bodies."""

    if hours not in {1, 2}:
        raise ValueError("log export period must be 1 or 2 hours")
    current = now or datetime.now(UTC)
    since = current - timedelta(hours=hours)
    rows: list[tuple[datetime, dict[str, Any]]] = []

    runtime_rows = list(
        await session.scalars(
            select(RuntimeHealth)
            .where(RuntimeHealth.heartbeat_at >= since)
            .order_by(RuntimeHealth.heartbeat_at.desc())
            .limit(500)
        )
    )
    for item in runtime_rows:
        rows.append(
            (
                _aware(item.heartbeat_at),
                {
                    "timestamp": _aware(item.heartbeat_at).isoformat(),
                    "component": item.component,
                    "level": "INFO" if item.status == "healthy" else "WARNING",
                    "event": "runtime_heartbeat",
                    "status": item.status,
                },
            )
        )

    jobs = list(
        await session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.updated_at >= since,
                BackgroundJob.status.in_(
                    ("failed", "retry", "retry_scheduled", "cancelled")
                ),
            )
            .order_by(BackgroundJob.updated_at.desc())
            .limit(2000)
        )
    )
    for item in jobs:
        rows.append(
            (
                _aware(item.updated_at),
                {
                    "timestamp": _aware(item.updated_at).isoformat(),
                    "component": "worker",
                    "level": "ERROR" if item.status == "failed" else "WARNING",
                    "event": "background_job_state",
                    "status": item.status,
                    "job_id": item.id,
                    "tenant_id": item.tenant_id,
                    "correlation_id": item.correlation_id,
                    "stage": item.job_type,
                    "category": item.category,
                    "retry_count": item.attempts,
                    "error_code": _safe_code(item.last_error),
                },
            )
        )

    ai_calls = list(
        await session.scalars(
            select(AIUsageCall)
            .where(
                AIUsageCall.occurred_at >= since,
                AIUsageCall.status.not_in(("success", "completed")),
            )
            .order_by(AIUsageCall.occurred_at.desc())
            .limit(1000)
        )
    )
    for item in ai_calls:
        rows.append(
            (
                _aware(item.occurred_at),
                {
                    "timestamp": _aware(item.occurred_at).isoformat(),
                    "component": "ai_provider",
                    "level": "ERROR",
                    "event": "ai_call_failed",
                    "status": item.status,
                    "tenant_id": item.tenant_id,
                    "job_id": item.job_id,
                    "stage": item.job_type,
                    "duration_ms": item.duration_ms,
                    "error_code": _safe_code(item.error_code),
                },
            )
        )

    notifications = list(
        await session.scalars(
            select(NotificationLog)
            .where(
                NotificationLog.updated_at >= since,
                NotificationLog.status.in_(
                    ("failed", "cancelled", "delivery_uncertain")
                ),
            )
            .order_by(NotificationLog.updated_at.desc())
            .limit(1000)
        )
    )
    for item in notifications:
        rows.append(
            (
                _aware(item.updated_at),
                {
                    "timestamp": _aware(item.updated_at).isoformat(),
                    "component": "telegram_delivery",
                    "level": "ERROR" if item.status == "failed" else "WARNING",
                    "event": "notification_state",
                    "status": item.status,
                    "tenant_id": item.tenant_id,
                    "notification_id": item.id,
                    "destination_type": item.destination_type,
                    "error_code": _safe_code(item.last_error_code),
                },
            )
        )

    report_runs = list(
        await session.scalars(
            select(ReportGenerationRun)
            .where(
                ReportGenerationRun.updated_at >= since,
                ReportGenerationRun.delayed_reason.is_not(None),
            )
            .order_by(ReportGenerationRun.updated_at.desc())
            .limit(500)
        )
    )
    for item in report_runs:
        rows.append(
            (
                _aware(item.updated_at),
                {
                    "timestamp": _aware(item.updated_at).isoformat(),
                    "component": "reports",
                    "level": "WARNING",
                    "event": "report_delayed",
                    "status": item.status,
                    "tenant_id": item.tenant_id,
                    "report_id": item.report_id,
                    "error_code": _safe_code(item.delayed_reason),
                },
            )
        )

    selected: list[bytes] = []
    selected_size = 0
    newest_rows = sorted(rows, key=lambda item: item[0], reverse=True)[:MAX_EXPORT_ROWS]
    for _, payload in newest_rows:
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if selected_size + len(line) > MAX_EXPORT_BYTES:
            break
        selected.append(line)
        selected_size += len(line)
    output = bytearray().join(reversed(selected))
    if not output:
        output.extend(
            (
                json.dumps(
                    {
                        "timestamp": current.isoformat(),
                        "component": "platform",
                        "level": "INFO",
                        "event": "no_operational_events",
                        "period_hours": hours,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
    return bytes(output)


ALERT_TEXT = {
    "runtime_stale": "Один или несколько процессов Ventrix перестали обновлять heartbeat.",
    "queue_backlog": "Возраст самой старой задачи в очереди превысил допустимый порог.",
    "delivery_failures": "За последний час выросло число ошибок Telegram-доставки.",
    "ai_provider": "За последний час обнаружены ошибки авторизации или квоты AI-провайдера.",
    "sqlite_locks": "Зафиксирована серия конфликтов записи SQLite.",
    "disk_low": "На диске сервера осталось мало свободного места.",
}


class PlatformOwnerAlertDispatcher:
    def __init__(
        self,
        *,
        api_base_url: str,
        bot_token: str,
        owner_telegram_id: int,
        timeout_seconds: int = 20,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.bot_token = bot_token
        self.owner_telegram_id = owner_telegram_id
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, job: JobLease) -> dict[str, str]:
        code = str(job.payload.get("code") or "")
        state = str(job.payload.get("state") or "active")
        if code not in ALERT_TEXT or state not in {"active", "recovered"}:
            raise ValueError("unsupported platform alert")
        prefix = "🔴 <b>Системный алерт</b>" if state == "active" else "🟢 <b>Восстановлено</b>"
        text = f"{prefix}\n\n{ALERT_TEXT[code]}\n\nКод: <code>{code}</code>"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.api_base_url}/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.owner_telegram_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
        finally:
            # Do not retain another local reference longer than the request.
            text = ""
        data = response.json() if response.content else {}
        if response.status_code == 429:
            retry_after = int((data.get("parameters") or {}).get("retry_after", 30))
            raise JobDeferred(max(1, min(retry_after, 3600)), "platform_alert_rate_limited")
        if response.is_error or data.get("ok") is not True:
            error = RuntimeError(f"platform_alert_delivery_failed_{response.status_code}")
            if 400 <= response.status_code < 500:
                error.retryable = False  # type: ignore[attr-defined]
            raise error
        return {"status": "sent", "code": code, "state": state}


class PlatformAlertMonitor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
        *,
        backlog_age_seconds: int,
        delivery_failure_count: int,
        sqlite_lock_count: int,
        disk_free_percent: float,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.backlog_age_seconds = backlog_age_seconds
        self.delivery_failure_count = delivery_failure_count
        self.sqlite_lock_count = sqlite_lock_count
        self.disk_free_percent = disk_free_percent

    async def evaluate(self, now: datetime | None = None) -> list[dict[str, str]]:
        current = now or datetime.now(UTC)
        async with self.session_factory() as session:
            metrics = await collect_runtime_metrics(session)
            monitor = await session.scalar(
                select(RuntimeHealth).where(RuntimeHealth.component == "platform_monitor")
            )
            previous = set((monitor.details_json if monitor else {}).get("active_alerts") or [])
            sequence = int((monitor.details_json if monitor else {}).get("sequence") or 0)
            runtime_components = [
                item
                for item in metrics["runtime"]["components"]
                if item["component"] != "platform_monitor"
            ]
            ai_codes = set(metrics["ai"]["error_codes_last_hour"])
            disk_free = metrics["host"]["disk_free_percent"]
            conditions = {
                "runtime_stale": any(item["status"] != "healthy" for item in runtime_components),
                "queue_backlog": (
                    metrics["queue"]["oldest_job_age_seconds"]
                    >= self.backlog_age_seconds
                ),
                "delivery_failures": (
                    metrics["notifications"]["failures_last_hour"]
                    >= self.delivery_failure_count
                ),
                "ai_provider": bool(
                    ai_codes
                    & {
                        "deepseek_http_401",
                        "deepseek_http_402",
                        "deepseek_http_403",
                        "deepseek_http_429",
                    }
                ),
                "sqlite_locks": (
                    metrics["sqlite"]["lock_failures_last_hour"]
                    >= self.sqlite_lock_count
                ),
                "disk_low": (
                    disk_free is not None and disk_free <= self.disk_free_percent
                ),
            }
            active = {code for code, enabled in conditions.items() if enabled}
            transitions = [
                *[(code, "active") for code in sorted(active - previous)],
                *[(code, "recovered") for code in sorted(previous - active)],
            ]

        queued: list[dict[str, str]] = []
        for offset, (code, state) in enumerate(transitions, start=1):
            transition_sequence = sequence + offset
            await self.queue.enqueue(
                "platform.alert",
                {"code": code, "state": state},
                priority=1,
                category="notification",
                cost_class="light",
                idempotency_key=f"platform-alert:{transition_sequence}:{code}:{state}",
            )
            queued.append({"code": code, "state": state})

        # Persist the transition only after all notification jobs are durable. If the
        # process stops between enqueue and this write, deterministic idempotency keys
        # make the next evaluation safe and the alert is not lost.
        async with self.session_factory() as session:
            monitor = await session.scalar(
                select(RuntimeHealth).where(RuntimeHealth.component == "platform_monitor")
            )
            if monitor is None:
                monitor = RuntimeHealth(component="platform_monitor")
                session.add(monitor)
            monitor.status = "healthy" if not active else "attention"
            monitor.heartbeat_at = current
            monitor.details_json = {
                "active_alerts": sorted(active),
                "sequence": sequence + len(transitions),
            }
            await session.commit()
        return queued
