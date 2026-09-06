from __future__ import annotations

import os
import resource
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AIUsageCall,
    BackgroundJob,
    NotificationLog,
    Report,
    RuntimeHealth,
    Signal,
    TelegramConnection,
    TelegramMessage,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _latency_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (_utc(end) - _utc(start)).total_seconds() * 1000)


def percentiles(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None}

    def pick(percentile: float) -> float:
        index = round((len(ordered) - 1) * percentile)
        return round(ordered[index], 2)

    return {"p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99)}


def _host_metrics() -> dict[str, float | int | None]:
    try:
        disk = shutil.disk_usage(Path.cwd())
        disk_free_percent = round(disk.free / disk.total * 100, 2) if disk.total else None
    except OSError:
        disk = None
        disk_free_percent = None
    total_memory: int | None = None
    available_memory: int | None = None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_memory = int(os.sysconf("SC_PHYS_PAGES")) * page_size
        if "SC_AVPHYS_PAGES" in os.sysconf_names:
            available_memory = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
    except (OSError, TypeError, ValueError):
        pass
    max_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # ru_maxrss is bytes on macOS and KiB on Linux.
    if os.uname().sysname != "Darwin":
        max_rss *= 1024
    return {
        "disk_total_bytes": disk.total if disk else None,
        "disk_free_bytes": disk.free if disk else None,
        "disk_free_percent": disk_free_percent,
        "memory_total_bytes": total_memory,
        "memory_available_bytes": available_memory,
        "process_max_rss_bytes": max_rss,
    }


async def collect_runtime_metrics(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(UTC)
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)
    active_statuses = (
        "pending",
        "scheduled",
        "waiting",
        "retry",
        "retry_scheduled",
        "running",
    )
    active_by_category_rows = (
        await session.execute(
            select(
                BackgroundJob.category,
                func.count(BackgroundJob.id),
                func.min(BackgroundJob.created_at),
            )
            .where(BackgroundJob.status.in_(active_statuses))
            .group_by(BackgroundJob.category)
        )
    ).all()
    depth_by_category = {
        str(category): int(count) for category, count, _ in active_by_category_rows
    }
    age_by_category = {
        str(category): round((now - _utc(oldest)).total_seconds(), 3)
        for category, _, oldest in active_by_category_rows
        if oldest is not None
    }
    oldest = min(
        (_utc(item[2]) for item in active_by_category_rows if item[2] is not None),
        default=None,
    )
    running_by_worker = dict(
        (
            await session.execute(
                select(BackgroundJob.locked_by, func.count(BackgroundJob.id))
                .where(
                    BackgroundJob.status == "running",
                    BackgroundJob.locked_by.is_not(None),
                )
                .group_by(BackgroundJob.locked_by)
            )
        ).all()
    )
    jobs = (
        await session.execute(
            select(
                BackgroundJob.started_at,
                BackgroundJob.finished_at,
                BackgroundJob.category,
                BackgroundJob.attempts,
                BackgroundJob.status,
                BackgroundJob.updated_at,
                BackgroundJob.last_error,
            )
            .order_by(BackgroundJob.created_at.desc())
            .limit(2000)
        )
    ).all()
    completed = [item for item in jobs if item.started_at and item.finished_at]
    job_durations = [
        value
        for item in completed
        if (value := _latency_ms(item.started_at, item.finished_at)) is not None
    ]
    telegram_durations = [
        value
        for item in completed
        if item.category in {"sync", "telegram"}
        and (value := _latency_ms(item.started_at, item.finished_at)) is not None
    ]
    attempted = [item for item in jobs if item.attempts > 0]

    ai_calls = (
        await session.execute(
            select(
                AIUsageCall.status,
                AIUsageCall.error_code,
                AIUsageCall.occurred_at,
                AIUsageCall.job_type,
                AIUsageCall.duration_ms,
            )
            .where(AIUsageCall.occurred_at >= one_day_ago)
            .order_by(AIUsageCall.occurred_at.desc())
            .limit(2000)
        )
    ).all()
    message_signal_pairs = (
        await session.execute(
            select(TelegramMessage.sent_at, Signal.detected_at)
            .join(Signal, Signal.source_message_id == TelegramMessage.id)
            .order_by(Signal.detected_at.desc())
            .limit(2000)
        )
    ).all()
    signal_notification_pairs = (
        await session.execute(
            select(Signal.detected_at, NotificationLog.sent_at)
            .join(NotificationLog, NotificationLog.signal_id == Signal.id)
            .where(NotificationLog.sent_at.is_not(None))
            .order_by(NotificationLog.sent_at.desc())
            .limit(2000)
        )
    ).all()
    overdue_reports = int(
        await session.scalar(
            select(func.count(Report.id)).where(
                Report.due_at.is_not(None),
                Report.due_at < now,
                Report.status != "ready",
            )
        )
        or 0
    )
    notification_failures = int(
        await session.scalar(
            select(func.count(NotificationLog.id)).where(NotificationLog.status == "failed")
        )
        or 0
    )
    recent_notification_failures = int(
        await session.scalar(
            select(func.count(NotificationLog.id)).where(
                NotificationLog.status.in_(("failed", "delivery_uncertain")),
                NotificationLog.updated_at >= one_hour_ago,
            )
        )
        or 0
    )
    recent_job_failures = int(
        await session.scalar(
            select(func.count(BackgroundJob.id)).where(
                BackgroundJob.status == "failed",
                BackgroundJob.updated_at >= one_hour_ago,
            )
        )
        or 0
    )
    successful_ai_statuses = {"success", "completed"}
    ai_errors = [item for item in ai_calls if item.status not in successful_ai_statuses]
    recent_ai_errors = [
        item for item in ai_errors if _utc(item.occurred_at) >= one_hour_ago
    ]
    invalid_json = [item for item in ai_errors if item.error_code == "invalid_json"]
    recent_operational_errors = [
        item
        for item in jobs
        if item.updated_at is not None and _utc(item.updated_at) >= one_hour_ago
    ]
    flood_waits = [
        item
        for item in recent_operational_errors
        if "flood" in (item.last_error or "").lower()
    ]
    sqlite_locks = [
        item
        for item in recent_operational_errors
        if "database is locked" in (item.last_error or "").lower()
    ]
    connections = (
        await session.execute(
            select(
                TelegramConnection.runtime_status,
                TelegramConnection.updates_received,
                TelegramConnection.duplicate_events,
                TelegramConnection.catchup_events,
                TelegramConnection.runtime_heartbeat_at,
            ).where(TelegramConnection.deleted_at.is_(None))
        )
    ).all()
    runtime_rows = (
        await session.execute(
            select(
                RuntimeHealth.component,
                RuntimeHealth.status,
                RuntimeHealth.heartbeat_at,
                RuntimeHealth.details_json,
            )
        )
    ).all()
    worker_rows = [
        item
        for item in runtime_rows
        if item.component == "worker" or item.component.startswith("worker:")
    ]
    if worker_rows:
        current_worker = max(worker_rows, key=lambda item: _utc(item.heartbeat_at))
        runtime_rows = [
            item
            for item in runtime_rows
            if item.component != "worker" and not item.component.startswith("worker:")
        ]
        runtime_rows.append(current_worker)
    runtime_components = [
        {
            "component": item.component,
            "reported_status": item.status,
            "status": (
                "stale"
                if item.status == "healthy"
                and (now - _utc(item.heartbeat_at)).total_seconds() > 120
                else item.status
            ),
            "heartbeat_at": _utc(item.heartbeat_at),
            "heartbeat_age_seconds": round(
                max(0.0, (now - _utc(item.heartbeat_at)).total_seconds()), 3
            ),
            "details": item.details_json,
        }
        for item in runtime_rows
    ]
    ai_by_job_type: dict[str, dict[str, int]] = {}
    for call in ai_calls:
        row = ai_by_job_type.setdefault(
            call.job_type or "unknown", {"calls": 0, "errors": 0, "duration_ms": 0}
        )
        row["calls"] += 1
        row["errors"] += call.status not in successful_ai_statuses
        row["duration_ms"] += int(call.duration_ms or 0)

    return {
        "generated_at": now,
        "queue": {
            "depth": sum(depth_by_category.values()),
            "depth_by_category": depth_by_category,
            "oldest_age_by_category_seconds": age_by_category,
            "oldest_job_age_seconds": round((now - oldest).total_seconds(), 3) if oldest else 0,
            "running_by_worker": {str(key): int(value) for key, value in running_by_worker.items()},
        },
        "jobs": {
            "duration_ms": percentiles(job_durations),
            "retry_rate": round(sum(item.attempts > 1 for item in attempted) / len(attempted), 4)
            if attempted
            else 0,
            "failure_rate": round(sum(item.status == "failed" for item in jobs) / len(jobs), 4)
            if jobs
            else 0,
            "failures_last_hour": recent_job_failures,
        },
        "telegram": {
            "fetch_latency_ms": percentiles(telegram_durations),
            "flood_wait_failures": len(flood_waits),
            "runtime_status": dict(Counter(item.runtime_status for item in connections)),
            "updates_received": sum(item.updates_received for item in connections),
            "duplicate_events": sum(item.duplicate_events for item in connections),
            "catchup_events": sum(item.catchup_events for item in connections),
            "heartbeat_lag_seconds": percentiles(
                [
                    max(0.0, (now - _utc(item.runtime_heartbeat_at)).total_seconds())
                    for item in connections
                    if item.runtime_heartbeat_at
                ]
            ),
        },
        "pipeline": {
            "message_to_signal_ms": percentiles(
                [
                    value
                    for start, end in message_signal_pairs
                    if (value := _latency_ms(start, end)) is not None
                ]
            ),
            "signal_to_notification_ms": percentiles(
                [
                    value
                    for start, end in signal_notification_pairs
                    if (value := _latency_ms(start, end)) is not None
                ]
            ),
        },
        "ai": {
            "latency_ms": percentiles([float(item.duration_ms) for item in ai_calls]),
            "errors": len(ai_errors),
            "errors_last_hour": len(recent_ai_errors),
            "error_codes_last_hour": dict(
                Counter(item.error_code or "unknown" for item in recent_ai_errors)
            ),
            "invalid_json": len(invalid_json),
            "by_job_type": ai_by_job_type,
        },
        "sqlite": {"lock_failures_last_hour": len(sqlite_locks)},
        "reports": {"overdue": overdue_reports},
        "notifications": {
            "failures": notification_failures,
            "failures_last_hour": recent_notification_failures,
        },
        "runtime": {
            "components": runtime_components,
            "stale": sum(item["status"] == "stale" for item in runtime_components),
        },
        "host": _host_metrics(),
    }
