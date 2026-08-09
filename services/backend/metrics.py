from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AIUsageCall,
    BackgroundJob,
    NotificationLog,
    Report,
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


async def collect_runtime_metrics(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(UTC)
    jobs = list(
        await session.scalars(
            select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(2000)
        )
    )
    active_statuses = {"pending", "scheduled", "waiting", "retry", "retry_scheduled", "running"}
    active = [item for item in jobs if item.status in active_statuses]
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
    oldest = min((_utc(item.created_at) for item in active), default=None)
    attempted = [item for item in jobs if item.attempts > 0]

    ai_calls = list(
        await session.scalars(
            select(AIUsageCall).order_by(AIUsageCall.occurred_at.desc()).limit(2000)
        )
    )
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
    ai_errors = [item for item in ai_calls if item.status != "success"]
    invalid_json = [item for item in ai_errors if item.error_code == "invalid_json"]
    flood_waits = [item for item in jobs if "flood" in (item.last_error or "").lower()]
    sqlite_locks = [
        item for item in jobs if "database is locked" in (item.last_error or "").lower()
    ]
    connections = list(
        await session.scalars(
            select(TelegramConnection).where(TelegramConnection.deleted_at.is_(None))
        )
    )
    ai_by_job_type: dict[str, dict[str, int]] = {}
    for call in ai_calls:
        row = ai_by_job_type.setdefault(
            call.job_type or "unknown", {"calls": 0, "errors": 0, "duration_ms": 0}
        )
        row["calls"] += 1
        row["errors"] += call.status != "success"
        row["duration_ms"] += int(call.duration_ms or 0)

    return {
        "generated_at": now,
        "queue": {
            "depth": len(active),
            "depth_by_category": dict(Counter(item.category for item in active)),
            "oldest_job_age_seconds": round((now - oldest).total_seconds(), 3) if oldest else 0,
        },
        "jobs": {
            "duration_ms": percentiles(job_durations),
            "retry_rate": round(sum(item.attempts > 1 for item in attempted) / len(attempted), 4)
            if attempted
            else 0,
            "failure_rate": round(sum(item.status == "failed" for item in jobs) / len(jobs), 4)
            if jobs
            else 0,
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
            "invalid_json": len(invalid_json),
            "by_job_type": ai_by_job_type,
        },
        "sqlite": {"persisted_lock_failures": len(sqlite_locks)},
        "reports": {"overdue": overdue_reports},
        "notifications": {"failures": notification_failures},
    }
