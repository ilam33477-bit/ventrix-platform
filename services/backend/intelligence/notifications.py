from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JOB_PRIORITY, JobDeferred, JobLease, SQLiteJobQueue
from ..models import (
    BackgroundJob,
    BotInstance,
    Commitment,
    Employee,
    EncryptedSecret,
    GroupIntegration,
    InitialAnalysisRun,
    NotificationLog,
    OperationalProblem,
    Signal,
    TelegramDialog,
    TelegramMessage,
    Tenant,
    TenantSettings,
)
from ..services.encryption import EncryptionService
from ..timezones import timezone_info


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    notify_employee: bool
    notify_manager: bool
    notify_group: bool
    immediate: bool
    reason: str


class NotificationPolicyService:
    def decide(
        self,
        *,
        settings: TenantSettings,
        signal: Signal,
        employee: Employee | None,
        source_type: str,
        group: GroupIntegration | None,
        now: datetime,
    ) -> NotificationDecision:
        criticality = signal.criticality
        triage = (signal.metadata_json or {}).get("triage") or {}
        employee_requested = bool(triage.get("requires_employee_notification", True))
        manager_requested = bool(triage.get("requires_manager_notification", True))
        employee_allowed = bool(
            settings.employee_notifications_enabled
            and employee
            and employee.notifications_enabled
            and employee.telegram_user_id
            and criticality
            >= max(settings.employee_notification_threshold, employee.criticality_threshold)
            and employee_requested
            and not self._quiet(employee, settings.timezone, now)
        )
        manager_allowed = bool(
            criticality >= settings.manager_notification_threshold and manager_requested
        )
        group_allowed = bool(
            settings.group_reminders_enabled
            and source_type == "group"
            and group
            and group.status == "active"
            and group.notifications_enabled
            and criticality >= max(settings.group_notification_threshold, group.minimum_criticality)
        )
        immediate = criticality >= settings.notification_immediate_threshold
        return NotificationDecision(
            notify_employee=employee_allowed,
            notify_manager=manager_allowed,
            notify_group=group_allowed,
            immediate=immediate,
            reason="central policy thresholds, privacy, quiet hours and destination state",
        )

    @staticmethod
    def _quiet(employee: Employee, timezone: str, now: datetime) -> bool:
        if employee.quiet_hours_start is None or employee.quiet_hours_end is None:
            return False
        try:
            zone = timezone_info(timezone)
        except ValueError:
            zone = UTC
        local_time = now.astimezone(zone).time().replace(tzinfo=None)
        start = employee.quiet_hours_start
        end = employee.quiet_hours_end
        return start <= local_time < end if start < end else local_time >= start or local_time < end


class NotificationSender(Protocol):
    async def send(
        self,
        tenant_id: str,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None: ...


class TelegramBotNotificationSender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        api_base_url: str,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.api_base_url = api_base_url.rstrip("/")

    async def send(
        self,
        tenant_id: str,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        async with self.session_factory() as session:
            bot = await session.scalar(
                select(BotInstance)
                .where(
                    BotInstance.tenant_id == tenant_id,
                    BotInstance.enabled.is_(True),
                    BotInstance.deleted_at.is_(None),
                )
                .order_by(BotInstance.created_at.desc())
                .limit(1)
            )
            if bot is None:
                raise RuntimeError("active client bot is unavailable")
            secret = await session.get(EncryptedSecret, bot.secret_id)
            token = self.encryption.decrypt(secret.ciphertext)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.api_base_url}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        **({"reply_markup": reply_markup} if reply_markup else {}),
                    },
                )
                response.raise_for_status()
        finally:
            token = ""


class NotificationOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
        *,
        cooldown_minutes: int = 120,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.cooldown_minutes = cooldown_minutes
        self.transactions = SQLiteTransactionManager(session_factory)
        self.policy = NotificationPolicyService()

    async def plan_for_signal(self, signal_id: str, problem_id: str | None = None) -> list[str]:
        async with self.session_factory() as session:
            signal = await session.get(Signal, signal_id)
            if signal is None or signal.status == "suppressed":
                return []
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == signal.tenant_id)
            )
            tenant = await session.get(Tenant, signal.tenant_id)
            employee = (
                await session.get(Employee, signal.employee_id) if signal.employee_id else None
            )
            dialog = await session.get(TelegramDialog, signal.dialog_id)
            if dialog is None or dialog.excluded or dialog.classification == "automated_account":
                return []
            source_message = (
                await session.get(TelegramMessage, signal.source_message_id)
                if signal.source_message_id
                else None
            )
            if source_message and source_message.ingestion_source == "history":
                # Initial backfill is summarized once after the run. Historical
                # findings remain visible in Mini App and never fan out alerts.
                return []
            if (signal.metadata_json or {}).get("source") == "scheduled_analysis":
                return []
            group = None
            if dialog.dialog_type == "group":
                group = await session.scalar(
                    select(GroupIntegration).where(
                        GroupIntegration.tenant_id == signal.tenant_id,
                        GroupIntegration.telegram_chat_id == dialog.telegram_dialog_id,
                    )
                )
            decision = self.policy.decide(
                settings=settings,
                signal=signal,
                employee=employee,
                source_type=dialog.dialog_type,
                group=group,
                now=datetime.now(UTC),
            )
            problem = await session.get(OperationalProblem, problem_id) if problem_id else None
            destinations: list[tuple[str, str, str | None]] = []
            if decision.notify_employee and employee and employee.telegram_user_id:
                destinations.append(("employee", str(employee.telegram_user_id), employee.id))
            if decision.notify_manager:
                destinations.append(("manager", str(tenant.owner_telegram_user_id), None))
            if decision.notify_group and group:
                destinations.append(("group", str(group.telegram_chat_id), None))

        notification_ids: list[str] = []
        for destination_type, destination_id, employee_id in destinations:
            notification_id = await self._create_log(
                signal,
                problem,
                group if destination_type == "group" else None,
                destination_type,
                destination_id,
                employee_id,
                bypass_cooldown=decision.immediate,
            )
            if notification_id:
                notification_ids.append(notification_id)
                await self.queue.enqueue(
                    f"notification.{destination_type}",
                    {"notification_id": notification_id},
                    tenant_id=signal.tenant_id,
                    priority=JOB_PRIORITY["P0"] if decision.immediate else JOB_PRIORITY["P1"],
                    idempotency_key=f"notification-job:{notification_id}",
                    correlation_id=signal.id,
                    is_heavy=False,
                    category="notification",
                    cost_class="light",
                    max_attempts=5,
                )
        return notification_ids

    async def _create_log(
        self,
        signal: Signal,
        problem: OperationalProblem | None,
        group: GroupIntegration | None,
        destination_type: str,
        destination_id: str,
        employee_id: str | None,
        *,
        bypass_cooldown: bool,
    ) -> str | None:
        dedup_key = f"signal:{signal.id}:{destination_type}:{destination_id}"

        async def write(session: AsyncSession) -> str | None:
            now = datetime.now(UTC)
            if await session.scalar(
                select(NotificationLog.id).where(NotificationLog.deduplication_key == dedup_key)
            ):
                return None
            severity_floor = (signal.criticality // 10) * 10
            if not bypass_cooldown:
                recent_equivalent = await session.scalar(
                    select(NotificationLog.id)
                    .join(Signal, Signal.id == NotificationLog.signal_id)
                    .where(
                        NotificationLog.tenant_id == signal.tenant_id,
                        NotificationLog.destination_type == destination_type,
                        NotificationLog.destination_id == destination_id,
                        NotificationLog.cooldown_until.is_not(None),
                        NotificationLog.cooldown_until > now,
                        NotificationLog.status.in_(("pending", "sent")),
                        Signal.dialog_id == signal.dialog_id,
                        Signal.signal_type == signal.signal_type,
                        NotificationLog.criticality >= severity_floor,
                        NotificationLog.criticality < min(101, severity_floor + 10),
                    )
                )
                if recent_equivalent:
                    return None
            is_group = destination_type == "group"
            title = self._title(signal.signal_type)
            provisional = bool(
                (signal.metadata_json or {}).get("fast_lane")
                and not (signal.metadata_json or {}).get("triage")
            )
            prefix = "Предварительный критический сигнал: " if provisional else ""
            current_dialog = await session.get(TelegramDialog, signal.dialog_id)
            tenant_settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == signal.tenant_id)
            )
            source = (
                await session.get(TelegramMessage, signal.source_message_id)
                if signal.source_message_id
                else None
            )
            current_problem = await session.get(OperationalProblem, problem.id) if problem else None
            level = (
                "Критично"
                if signal.criticality >= 90
                else "Важно"
                if signal.criticality >= 75
                else "Требует внимания"
            )
            if is_group:
                text = (
                    f"⚠️ <b>{escape(prefix + title)}</b>\n\n"
                    f"{level}. Откройте Ventrix, чтобы проверить ситуацию и назначить ответственного."
                )
            else:
                person = current_dialog.title or "Собеседник"
                username = (source.sender_username if source else None) or current_dialog.username
                telegram_id = source.sender_id if source else None
                body = ((source.body_text if source else None) or "Сообщение без текста")[:1200]
                context = (
                    (current_problem.explanation if current_problem else None)
                    or signal.reason
                    or "Ситуация подтверждена анализом диалога."
                )[:1000]
                try:
                    event_time = signal.detected_at.astimezone(
                        timezone_info(tenant_settings.timezone)
                    )
                except (ValueError, AttributeError):
                    event_time = signal.detected_at
                destination_intro = (
                    f"В переписке с {escape(person)} требуется ваше внимание.\n\n"
                    if destination_type == "employee"
                    else ""
                )
                text = (
                    f"⚠️ <b>{escape(prefix + title)}</b>\n\n"
                    f"{destination_intro}"
                    f"<b>Клиент:</b> {escape(person)}\n"
                    f"<b>Username:</b> {escape('@' + username) if username else 'не указан'}\n"
                    f"<b>Telegram ID:</b> <code>{telegram_id or 'неизвестен'}</code>\n"
                    f"<b>Чат:</b> {escape(current_dialog.title or person)}\n\n"
                    f"<b>Причина:</b>\n{escape(signal.reason)}\n\n"
                    f"<b>Исходное сообщение:</b>\n<blockquote>{escape(body)}</blockquote>\n\n"
                    f"<b>Контекст:</b>\n<blockquote>{escape(context)}</blockquote>\n\n"
                    "<b>Следующий шаг:</b> Ответить клиенту или уточнить статус у ответственного.\n\n"
                    f"<i>{event_time:%d.%m.%Y %H:%M}</i>"
                )
            rows: list[list[dict[str, str]]] = []
            if current_problem:
                rows.append(
                    [
                        {
                            "text": "Открыть карточку",
                            "callback_data": f"np:open:{current_problem.id}",
                        },
                        {"text": "Закрыть", "callback_data": f"np:close:{current_problem.id}"},
                    ]
                )
                if destination_type == "manager":
                    rows.append(
                        [
                            {
                                "text": "Не проблема",
                                "callback_data": f"np:false:{current_problem.id}",
                            },
                            {
                                "text": "Уведомить сотрудника",
                                "callback_data": f"np:notify:{current_problem.id}",
                            },
                        ]
                    )
            username = current_dialog.username if current_dialog else None
            if username:
                message_url = (
                    f"https://t.me/{username}/{source.telegram_message_id}"
                    if source and current_dialog.dialog_type in {"group", "channel"}
                    else f"https://t.me/{username}"
                )
                rows.append([{"text": "Открыть чат", "url": message_url}])
            log = NotificationLog(
                tenant_id=signal.tenant_id,
                signal_id=signal.id,
                problem_id=problem.id if problem else None,
                employee_id=employee_id,
                group_integration_id=group.id if group else None,
                destination_type=destination_type,
                destination_id=destination_id,
                deduplication_key=dedup_key,
                criticality=signal.criticality,
                payload_json={
                    "text": text,
                    "privacy_safe": is_group,
                    "provisional": provisional,
                    "reply_markup": {"inline_keyboard": rows} if rows else None,
                },
                cooldown_until=now + timedelta(minutes=self.cooldown_minutes),
            )
            session.add(log)
            await session.flush()
            return log.id

        return await self.transactions.run(write)

    async def reconcile_provisional(
        self,
        signal_id: str,
        *,
        confirmed: bool,
        confirmed_criticality: int,
    ) -> list[str]:
        """Update pending provisional alerts or issue a correction after delivery."""

        async def write(session: AsyncSession) -> list[str]:
            logs = list(
                await session.scalars(
                    select(NotificationLog).where(NotificationLog.signal_id == signal_id)
                )
            )
            correction_ids: list[str] = []
            for log in logs:
                if not (log.payload_json or {}).get("provisional"):
                    continue
                if confirmed:
                    log.criticality = confirmed_criticality
                    log.payload_json = {
                        **log.payload_json,
                        "provisional": False,
                        "confirmed": True,
                        "text": str(log.payload_json["text"]).replace(
                            "Предварительный критический сигнал: ", ""
                        ),
                    }
                    continue
                if log.status == "pending":
                    log.status = "cancelled"
                    log.payload_json = {**log.payload_json, "provisional": False, "cancelled": True}
                    continue
                if log.status != "sent":
                    continue
                correction_key = f"{log.deduplication_key}:correction"
                exists = await session.scalar(
                    select(NotificationLog.id).where(
                        NotificationLog.deduplication_key == correction_key
                    )
                )
                if exists:
                    continue
                correction = NotificationLog(
                    tenant_id=log.tenant_id,
                    signal_id=log.signal_id,
                    problem_id=log.problem_id,
                    employee_id=log.employee_id,
                    group_integration_id=log.group_integration_id,
                    destination_type=log.destination_type,
                    destination_id=log.destination_id,
                    deduplication_key=correction_key,
                    criticality=confirmed_criticality,
                    payload_json={
                        "text": "ℹ️ Предварительный сигнал проверен: критическая ситуация не подтверждена.",
                        "privacy_safe": log.destination_type == "group",
                        "provisional": False,
                        "correction": True,
                    },
                )
                session.add(correction)
                await session.flush()
                correction_ids.append(correction.id)
            return correction_ids

        correction_ids = await self.transactions.run(write)
        async with self.session_factory() as session:
            corrections = (
                list(
                    await session.scalars(
                        select(NotificationLog).where(NotificationLog.id.in_(correction_ids))
                    )
                )
                if correction_ids
                else []
            )
        for correction in corrections:
            await self.queue.enqueue(
                f"notification.{correction.destination_type}",
                {"notification_id": correction.id},
                tenant_id=correction.tenant_id,
                priority=JOB_PRIORITY["P0"],
                idempotency_key=f"notification-job:{correction.id}",
                correlation_id=signal_id,
                category="notification",
                cost_class="light",
                max_attempts=5,
            )
        return correction_ids

    @staticmethod
    def _title(signal_type: str) -> str:
        return {
            "contract_question": "Клиент ждёт договор или КП",
            "payment_question": "Вопрос об оплате или реквизитах",
            "commercial_question": "Клиент спрашивает о стоимости",
            "new_lead": "Новый клиент готов двигаться дальше",
            "waiting_customer": "Клиент повторно ждёт ответа",
            "complaint": "Получена жалоба клиента",
            "overdue_commitment": "Просрочено обещание сотрудника",
        }.get(signal_type, "Ситуация требует внимания")


class NotificationDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sender: NotificationSender,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.transactions = SQLiteTransactionManager(session_factory)

    async def dispatch(self, job: JobLease) -> dict[str, str]:
        notification_id = str(job.payload["notification_id"])
        async with self.session_factory() as session:
            log = await session.scalar(
                select(NotificationLog).where(
                    NotificationLog.id == notification_id,
                    NotificationLog.tenant_id == job.tenant_id,
                )
            )
            if log is None:
                raise LookupError("notification not found in tenant")
            if log.status == "sent":
                return {"notification_id": log.id, "status": "sent"}
            tenant_id = log.tenant_id
            destination_id = log.destination_id
            text = str(log.payload_json["text"])
        try:
            reply_markup = log.payload_json.get("reply_markup")
            await self.sender.send(tenant_id, destination_id, text, reply_markup)
        except httpx.RequestError:
            # Telegram may have accepted the request before the response was
            # lost. Avoid an ambiguous retry that would duplicate the alert.
            await self._mark(notification_id, "delivery_uncertain", "network_ambiguous")
            return {"notification_id": notification_id, "status": "delivery_uncertain"}
        except Exception as exc:
            await self._mark(notification_id, "failed", type(exc).__name__)
            raise
        await self._mark(notification_id, "sent", None)
        return {"notification_id": notification_id, "status": "sent"}

    async def initial_summary(self, job: JobLease) -> dict[str, str | int]:
        run_id = str(job.payload["run_id"])
        async with self.session_factory() as session:
            run = await session.scalar(
                select(InitialAnalysisRun).where(
                    InitialAnalysisRun.id == run_id,
                    InitialAnalysisRun.tenant_id == job.tenant_id,
                )
            )
            if run is None:
                raise LookupError("initial analysis run not found")
            unfinished = int(
                await session.scalar(
                    select(func.count(BackgroundJob.id)).where(
                        BackgroundJob.correlation_id == run.id,
                        BackgroundJob.job_type == "signal.scan_batch",
                        BackgroundJob.status.in_(
                            (
                                "pending",
                                "scheduled",
                                "running",
                                "waiting",
                                "retry",
                                "retry_scheduled",
                            )
                        ),
                    )
                )
                or 0
            )
            if unfinished:
                raise JobDeferred(3, "waiting_for_historical_signal_scan")
            tenant = await session.get(Tenant, run.tenant_id)
            existing = await session.scalar(
                select(NotificationLog).where(
                    NotificationLog.deduplication_key
                    == f"initial-summary:{run.id}:manager:{tenant.owner_telegram_user_id}"
                )
            )
            signal_counts = dict(
                (
                    await session.execute(
                        select(Signal.signal_type, func.count(Signal.id))
                        .join(TelegramMessage, TelegramMessage.id == Signal.source_message_id)
                        .where(
                            Signal.tenant_id == run.tenant_id,
                            TelegramMessage.connection_id == run.connection_id,
                            TelegramMessage.ingestion_source == "history",
                        )
                        .group_by(Signal.signal_type)
                    )
                ).all()
            )
            problems = int(
                await session.scalar(
                    select(func.count(OperationalProblem.id)).where(
                        OperationalProblem.tenant_id == run.tenant_id,
                        OperationalProblem.connection_id == run.connection_id,
                    )
                )
                or 0
            )
            commitments = int(
                await session.scalar(
                    select(func.count(Commitment.id)).where(
                        Commitment.tenant_id == run.tenant_id,
                        Commitment.connection_id == run.connection_id,
                    )
                )
                or 0
            )
            total_signals = sum(int(value) for value in signal_counts.values())
            text = (
                "✅ <b>Первичная проверка завершена</b>\n\n"
                f"Сообщений обработано: <b>{run.messages_loaded}</b>\n"
                f"Ситуаций требуют внимания: <b>{problems}</b>\n"
                f"Обязательств найдено: <b>{commitments}</b>\n"
                f"Клиентов без ответа: <b>{int(signal_counts.get('client_without_answer', 0))}</b>\n"
                f"Жалоб: <b>{int(signal_counts.get('complaint', 0))}</b>\n"
                f"Всего рабочих сигналов: <b>{total_signals}</b>\n\n"
                "Откройте Ventrix, чтобы проверить карточки и назначить ответственных."
            )
            if existing is None:
                existing = NotificationLog(
                    tenant_id=run.tenant_id,
                    destination_type="manager",
                    destination_id=str(tenant.owner_telegram_user_id),
                    deduplication_key=f"initial-summary:{run.id}:manager:{tenant.owner_telegram_user_id}",
                    criticality=0,
                    payload_json={
                        "text": text,
                        "privacy_safe": True,
                        "reply_markup": {
                            "inline_keyboard": [
                                [{"text": "Открыть важное", "callback_data": "client:important"}]
                            ]
                        },
                    },
                )
                session.add(existing)
                await session.commit()
                await session.refresh(existing)
            if existing.status == "sent":
                return {"notification_id": existing.id, "status": "sent", "signals": total_signals}
            notification_id = existing.id
            destination_id = existing.destination_id
            reply_markup = existing.payload_json.get("reply_markup")
        try:
            await self.sender.send(run.tenant_id, destination_id, text, reply_markup)
        except httpx.RequestError:
            await self._mark(notification_id, "delivery_uncertain", "network_ambiguous")
            return {
                "notification_id": notification_id,
                "status": "delivery_uncertain",
                "signals": total_signals,
            }
        except Exception as exc:
            await self._mark(notification_id, "failed", type(exc).__name__)
            raise
        await self._mark(notification_id, "sent", None)
        return {"notification_id": notification_id, "status": "sent", "signals": total_signals}

    async def _mark(self, notification_id: str, status: str, error: str | None) -> None:
        async def write(session: AsyncSession) -> None:
            log = await session.get(NotificationLog, notification_id)
            log.status = status
            log.last_error_code = error
            if status == "sent":
                log.sent_at = datetime.now(UTC)

        await self.transactions.run(write)
