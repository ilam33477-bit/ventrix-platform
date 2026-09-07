from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..jobs.queue import JobDeferred, JobLease, SQLiteJobQueue
from ..metrics import collect_runtime_metrics
from ..models import (
    AIUsageCall,
    BackgroundJob,
    NotificationLog,
    OperationalProblem,
    ReportGenerationRun,
    RuntimeHealth,
    Signal,
    Tenant,
)
from ..observability import redact_log_text

MAX_EXPORT_ROWS = 5000
MAX_EXPORT_BYTES = 1_500_000
SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


LOG_GUIDANCE = {
    "runtime_heartbeat": (
        "Компонент подтвердил, что процесс запущен и имеет доступ к базе.",
        "Система не затронута, пока отметки продолжают поступать.",
        "Ничего делать не нужно.",
    ),
    "background_job_state": (
        "Фоновая задача не завершилась с первой попытки или была остановлена.",
        "Затронут только указанный этап; остальные проекты и функции продолжают работать.",
        "Если status=failed — откройте «Ошибки» в админ-боте. Для retry система повторит задачу сама.",
    ),
    "ai_call_failed": (
        "AI-провайдер не выполнил один запрос.",
        "Может задержаться анализ конкретного пакета сообщений или формирование отчёта.",
        "Проверьте error_code. При 429 дождитесь автоповтора; при 401/402/403 проверьте ключ или баланс.",
    ),
    "notification_state": (
        "Telegram не подтвердил доставку одного уведомления.",
        "Сама ситуация сохранена, но адресат мог не увидеть карточку в боте или группе.",
        "Проверьте доступ бота к получателю/группе и повторите отправку после восстановления доступа.",
    ),
    "report_delayed": (
        "Регулярный отчёт не был готов к запланированному времени.",
        "Затронут только указанный отчёт; мониторинг диалогов продолжает работать.",
        "Проверьте очередь и AI-ошибки. После устранения причины отчёт будет сформирован повторно.",
    ),
    "no_operational_events": (
        "За выбранный период ошибок, повторов и задержек не зарегистрировано.",
        "Все наблюдаемые системы работают штатно.",
        "Ничего делать не нужно.",
    ),
}


def _with_guidance(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    meaning, impact, action = LOG_GUIDANCE[event]
    return {**payload, "meaning": meaning, "impact": impact, "admin_action": action}


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
                _with_guidance(
                    "runtime_heartbeat",
                    {
                        "timestamp": _aware(item.heartbeat_at).isoformat(),
                        "component": item.component,
                        "level": "INFO" if item.status == "healthy" else "WARNING",
                        "event": "runtime_heartbeat",
                        "status": item.status,
                    },
                ),
            )
        )

    jobs = list(
        await session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.updated_at >= since,
                BackgroundJob.status.in_(("failed", "retry", "retry_scheduled", "cancelled")),
            )
            .order_by(BackgroundJob.updated_at.desc())
            .limit(2000)
        )
    )
    for item in jobs:
        rows.append(
            (
                _aware(item.updated_at),
                _with_guidance(
                    "background_job_state",
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
                ),
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
                _with_guidance(
                    "ai_call_failed",
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
                ),
            )
        )

    notifications = list(
        await session.scalars(
            select(NotificationLog)
            .where(
                NotificationLog.updated_at >= since,
                NotificationLog.status.in_(("failed", "cancelled", "delivery_uncertain")),
            )
            .order_by(NotificationLog.updated_at.desc())
            .limit(1000)
        )
    )
    for item in notifications:
        rows.append(
            (
                _aware(item.updated_at),
                _with_guidance(
                    "notification_state",
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
                ),
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
                _with_guidance(
                    "report_delayed",
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
                ),
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
                    _with_guidance(
                        "no_operational_events",
                        {
                            "timestamp": current.isoformat(),
                            "component": "platform",
                            "level": "INFO",
                            "event": "no_operational_events",
                            "period_hours": hours,
                        },
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
    return bytes(output)


ALERT_GUIDANCE = {
    "runtime_stale": {
        "title": "Компонент перестал подтверждать работу",
        "meaning": (
            "Heartbeat — это служебная отметка, которую процесс обновляет каждые несколько секунд. "
            "Если отметки нет больше двух минут, процесс мог остановиться или потерять доступ к базе."
        ),
        "impact": "Не работает только указанный компонент; остальные компоненты проверяются отдельно.",
        "action": (
            "Откройте «Состояние», посмотрите имя красного компонента. Если он не восстановится "
            "за 5 минут, перезапустите соответствующий сервис Ventrix."
        ),
    },
    "queue_backlog": {
        "title": "Задачи слишком долго ждут обработки",
        "meaning": "Самая старая готовая задача находится в очереди дольше установленного порога.",
        "impact": "Могут задерживаться анализ сообщений, ситуации или отчёты; новые данные не теряются.",
        "action": "Откройте «Ошибки» и проверьте категории очереди и состояние worker/AI-провайдера.",
    },
    "delivery_failures": {
        "title": "Telegram не доставляет часть уведомлений",
        "meaning": "За час Telegram отклонил несколько отправок карточек или отчётов.",
        "impact": "Ситуации сохранены, но отдельные сотрудники, администраторы или группы могли их не увидеть.",
        "action": "Проверьте, что бот не заблокирован и имеет право писать в подключённые группы.",
    },
    "ai_provider": {
        "title": "AI-провайдер отклоняет запросы",
        "meaning": "Получена ошибка доступа, баланса или лимита запросов AI-провайдера.",
        "impact": "Новые сообщения сохраняются, но их AI-анализ и отчёты могут ждать автоповтора.",
        "action": "Откройте «Ошибки»: 401/403 — проверить ключ, 402 — баланс, 429 — лимит и дождаться автоповтора.",
    },
    "sqlite_locks": {
        "title": "База данных временно занята",
        "meaning": "Несколько операций записи одновременно не смогли получить блокировку SQLite.",
        "impact": "Отдельные фоновые задачи могли перейти в автоматический повтор; данные не должны потеряться.",
        "action": "Проверьте очередь. Если ошибки продолжаются более 5 минут, снизьте нагрузку и проверьте worker.",
    },
    "disk_low": {
        "title": "Заканчивается место на сервере",
        "meaning": "Свободного дискового пространства меньше безопасного порога.",
        "impact": "При полном заполнении остановятся записи в базу, обработка сообщений, отчёты и логи.",
        "action": "Удалите только проверенные старые резервные копии/образы либо увеличьте диск. Не ждите заполнения до 100%.",
    },
}


def _alert_measurement(code: str, payload: dict[str, Any]) -> str:
    if code == "runtime_stale":
        components = [
            _safe_code(item) for item in payload.get("components", []) if _safe_code(item)
        ][:10]
        return "Не отвечают: " + (", ".join(components) if components else "не определено")
    if code == "queue_backlog":
        return f"Возраст старейшей задачи: {int(payload.get('oldest_seconds') or 0)} сек."
    if code == "delivery_failures":
        return f"Ошибок доставки за час: {int(payload.get('count') or 0)}."
    if code == "ai_provider":
        codes = [_safe_code(item) for item in payload.get("error_codes", []) if _safe_code(item)][
            :10
        ]
        return "Коды AI: " + (", ".join(codes) if codes else "не определены")
    if code == "sqlite_locks":
        return f"Конфликтов записи за час: {int(payload.get('count') or 0)}."
    if code == "disk_low":
        return f"Свободно на диске: {float(payload.get('free_percent') or 0):.1f}%."
    return ""


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
        if code not in ALERT_GUIDANCE or state not in {"active", "recovered"}:
            raise ValueError("unsupported platform alert")
        guidance = ALERT_GUIDANCE[code]
        measurement = _alert_measurement(code, job.payload)
        if state == "active":
            text = (
                f"🔴 <b>{guidance['title']}</b>\n\n"
                f"<b>Текущее состояние</b>\n{escape(measurement)}\n\n"
                f"<b>Что это значит</b>\n{guidance['meaning']}\n\n"
                f"<b>Что затронуто</b>\n{guidance['impact']}\n\n"
                f"<b>Что сделать</b>\n{guidance['action']}\n\n"
                f"Технический код: <code>{code}</code>"
            )
        else:
            text = (
                f"🟢 <b>Восстановлено: {guidance['title']}</b>\n\n"
                "Проверка снова проходит успешно. Система автоматически продолжает обработку; "
                "действия администратора не требуются.\n\n"
                f"Технический код: <code>{code}</code>"
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.api_base_url}/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.owner_telegram_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": {
                            "inline_keyboard": [
                                [
                                    {"text": "⚠️ Ошибки", "callback_data": "owner:system:errors"},
                                    {"text": "🟢 Состояние", "callback_data": "owner:system"},
                                ]
                            ]
                        },
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


async def build_platform_summary_text(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> str:
    """Build one bounded 24-hour owner summary across every active project."""

    current = now or datetime.now(UTC)
    since = current - timedelta(hours=24)
    tenants = list(
        await session.scalars(
            select(Tenant)
            .where(Tenant.deleted_at.is_(None), Tenant.status == "active")
            .order_by(Tenant.name)
        )
    )
    tenant_ids = [tenant.id for tenant in tenants]

    ai_rows = (
        await session.execute(
            select(
                AIUsageCall.tenant_id,
                func.coalesce(func.sum(AIUsageCall.input_tokens + AIUsageCall.output_tokens), 0),
                func.coalesce(func.sum(AIUsageCall.estimated_cost), 0.0),
                func.count(AIUsageCall.id),
                func.coalesce(
                    func.sum(
                        case(
                            (AIUsageCall.status.not_in(("success", "completed")), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .where(AIUsageCall.occurred_at >= since)
            .group_by(AIUsageCall.tenant_id)
        )
    ).all()
    problem_rows = (
        await session.execute(
            select(OperationalProblem.tenant_id, func.count(OperationalProblem.id))
            .where(OperationalProblem.created_at >= since)
            .group_by(OperationalProblem.tenant_id)
        )
    ).all()
    signal_rows = (
        await session.execute(
            select(Signal.tenant_id, func.count(Signal.id))
            .where(Signal.processed_at >= since)
            .group_by(Signal.tenant_id)
        )
    ).all()
    job_error_rows = (
        await session.execute(
            select(BackgroundJob.tenant_id, func.count(BackgroundJob.id))
            .where(BackgroundJob.updated_at >= since, BackgroundJob.status == "failed")
            .group_by(BackgroundJob.tenant_id)
        )
    ).all()
    delivery_error_rows = (
        await session.execute(
            select(NotificationLog.tenant_id, func.count(NotificationLog.id))
            .where(
                NotificationLog.updated_at >= since,
                NotificationLog.status.in_(("failed", "delivery_uncertain")),
            )
            .group_by(NotificationLog.tenant_id)
        )
    ).all()

    ai_by_tenant = {
        tenant_id: {
            "tokens": int(tokens or 0),
            "cost": float(cost or 0),
            "calls": int(calls or 0),
            "errors": int(errors or 0),
        }
        for tenant_id, tokens, cost, calls, errors in ai_rows
    }
    problems_by_tenant = {tenant_id: int(count) for tenant_id, count in problem_rows}
    signals_by_tenant = {tenant_id: int(count) for tenant_id, count in signal_rows}
    jobs_by_tenant = {tenant_id: int(count) for tenant_id, count in job_error_rows}
    delivery_by_tenant = {tenant_id: int(count) for tenant_id, count in delivery_error_rows}
    metrics = await collect_runtime_metrics(session)

    total_tokens = sum(row["tokens"] for row in ai_by_tenant.values())
    total_cost = sum(row["cost"] for row in ai_by_tenant.values())
    total_calls = sum(row["calls"] for row in ai_by_tenant.values())
    total_ai_errors = sum(row["errors"] for row in ai_by_tenant.values())
    total_problems = sum(problems_by_tenant.values())
    total_signals = sum(signals_by_tenant.values())
    total_job_errors = sum(jobs_by_tenant.values())
    total_delivery_errors = sum(delivery_by_tenant.values())
    total_errors = total_ai_errors + total_job_errors + total_delivery_errors
    queue_depth = int(metrics["queue"]["depth"] or 0)
    stale = int(metrics["runtime"]["stale"] or 0)
    disk_free = metrics["host"].get("disk_free_percent")

    project_lines = []
    for tenant in tenants[:12]:
        ai = ai_by_tenant.get(tenant.id, {"tokens": 0, "errors": 0})
        errors = (
            int(ai["errors"])
            + jobs_by_tenant.get(tenant.id, 0)
            + delivery_by_tenant.get(tenant.id, 0)
        )
        project_lines.append(
            f"• <b>{escape(tenant.name[:60])}</b>: "
            f"{int(ai['tokens']):,} токенов · "
            f"{problems_by_tenant.get(tenant.id, 0)} ситуаций · "
            f"{errors} ошибок"
        )
    if len(tenants) > 12:
        project_lines.append(f"• …и ещё {len(tenants) - 12} проектов")
    projects_text = "\n".join(project_lines) or "• активных проектов пока нет"
    health = (
        "🟢 штатно"
        if not stale and queue_depth == 0 and total_errors == 0
        else "🟠 требуется внимание"
    )
    disk_text = f"{float(disk_free):.1f}%" if disk_free is not None else "нет данных"

    return (
        "📊 <b>Ventrix · сводка SaaS за 24 часа</b>\n\n"
        f"Активных проектов: <b>{len(tenant_ids)}</b>\n"
        f"Проверено сигналов: <b>{total_signals}</b>\n"
        f"Создано рабочих ситуаций: <b>{total_problems}</b>\n\n"
        f"<b>AI</b>\n"
        f"Запросов: <b>{total_calls}</b>\n"
        f"Токенов: <b>{total_tokens:,}</b>\n"
        f"Оценочная стоимость: <b>{total_cost:.4f}</b>\n\n"
        f"<b>Ошибки за период</b>\n"
        f"AI: <b>{total_ai_errors}</b> · задачи: <b>{total_job_errors}</b> · "
        f"доставка: <b>{total_delivery_errors}</b>\n\n"
        f"<b>Система сейчас</b>: {health}\n"
        f"Очередь: <b>{queue_depth}</b> · просроченных heartbeat: <b>{stale}</b> · "
        f"свободно на диске: <b>{disk_text}</b>\n\n"
        f"<b>По проектам</b>\n{projects_text}"
    )


class PlatformOwnerSummaryDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        api_base_url: str,
        bot_token: str,
        owner_telegram_id: int,
        timeout_seconds: int = 20,
    ) -> None:
        self.session_factory = session_factory
        self.api_base_url = api_base_url.rstrip("/")
        self.bot_token = bot_token
        self.owner_telegram_id = owner_telegram_id
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, job: JobLease) -> dict[str, str]:
        async with self.session_factory() as session:
            summary = await build_platform_summary_text(session)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.api_base_url}/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.owner_telegram_id,
                        "text": summary,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": {
                            "inline_keyboard": [
                                [
                                    {"text": "⚠️ Ошибки", "callback_data": "owner:system:errors"},
                                    {"text": "🟢 Состояние", "callback_data": "owner:system"},
                                ],
                                [{"text": "↻ Обновить сводку", "callback_data": "owner:activity"}],
                            ]
                        },
                    },
                )
        finally:
            summary = ""
        data = response.json() if response.content else {}
        if response.status_code == 429:
            retry_after = int((data.get("parameters") or {}).get("retry_after", 30))
            raise JobDeferred(max(1, min(retry_after, 3600)), "platform_summary_rate_limited")
        if response.is_error or data.get("ok") is not True:
            error = RuntimeError(f"platform_summary_delivery_failed_{response.status_code}")
            if 400 <= response.status_code < 500:
                error.retryable = False  # type: ignore[attr-defined]
            raise error
        return {"status": "sent", "period": str(job.payload.get("period") or "24h")}


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
                    metrics["queue"]["oldest_job_age_seconds"] >= self.backlog_age_seconds
                ),
                "delivery_failures": (
                    metrics["notifications"]["failures_last_hour"] >= self.delivery_failure_count
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
                    metrics["sqlite"]["lock_failures_last_hour"] >= self.sqlite_lock_count
                ),
                "disk_low": (disk_free is not None and disk_free <= self.disk_free_percent),
            }
            active = {code for code, enabled in conditions.items() if enabled}
            details = {
                "runtime_stale": {
                    "components": [
                        str(item["component"])
                        for item in runtime_components
                        if item["status"] != "healthy"
                    ]
                },
                "queue_backlog": {
                    "oldest_seconds": int(metrics["queue"]["oldest_job_age_seconds"] or 0)
                },
                "delivery_failures": {
                    "count": int(metrics["notifications"]["failures_last_hour"] or 0)
                },
                "ai_provider": {"error_codes": sorted(ai_codes)},
                "sqlite_locks": {"count": int(metrics["sqlite"]["lock_failures_last_hour"] or 0)},
                "disk_low": {"free_percent": float(disk_free or 0)},
            }
            transitions = [
                *[(code, "active") for code in sorted(active - previous)],
                *[(code, "recovered") for code in sorted(previous - active)],
            ]

        queued: list[dict[str, str]] = []
        for offset, (code, state) in enumerate(transitions, start=1):
            transition_sequence = sequence + offset
            await self.queue.enqueue(
                "platform.alert",
                {"code": code, "state": state, **details[code]},
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
