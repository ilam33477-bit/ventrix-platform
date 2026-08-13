from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from ..models import (
    AIUsageCall,
    Commitment,
    DialogState,
    Employee,
    EmployeeTelegramAccount,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantSettings,
)
from .local_signals import LocalSignalCandidate, LocalSignalEngine
from .message_relevance import classify_message_relevance
from .notifications import NotificationOrchestrator


class SignalService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.transactions = SQLiteTransactionManager(session_factory)
        self.engine = LocalSignalEngine()
        self.notifications = NotificationOrchestrator(session_factory, queue)

    async def local_scan_job(self, job: JobLease) -> dict[str, int]:
        message_id = str(job.payload["message_id"])

        async def write(session: AsyncSession) -> list[str]:
            message = await session.scalar(
                select(TelegramMessage)
                .join(TelegramDialog, TelegramDialog.id == TelegramMessage.dialog_id)
                .where(
                    TelegramMessage.id == message_id,
                    TelegramMessage.tenant_id == job.tenant_id,
                    TelegramDialog.selected.is_(True),
                    TelegramDialog.excluded.is_(False),
                    TelegramDialog.classification != "automated_account",
                )
            )
            if message is None:
                return []
            previous = list(
                await session.scalars(
                    select(TelegramMessage)
                    .where(
                        TelegramMessage.dialog_id == message.dialog_id,
                        TelegramMessage.telegram_message_id < message.telegram_message_id,
                    )
                    .order_by(TelegramMessage.sent_at.desc())
                    .limit(10)
                )
            )
            previous.reverse()
            return [item.id for item in await self.scan_message(session, message, previous)]

        signal_ids = await self.transactions.run(write)
        await self._schedule_temporal_jobs([message_id])
        async with self.session_factory() as session:
            signals = list(await session.scalars(select(Signal).where(Signal.id.in_(signal_ids))))
        jobs = await self.enqueue_triage(signals)
        return {"signals": len(signal_ids), "triage_jobs": len(jobs)}

    async def scan_batch_job(self, job: JobLease) -> dict[str, int]:
        message_ids = [str(item) for item in job.payload.get("message_ids", [])][:200]
        if not message_ids:
            return {"signals": 0, "triage_jobs": 0}

        async def write(session: AsyncSession) -> list[str]:
            messages = list(
                await session.scalars(
                    select(TelegramMessage)
                    .join(TelegramDialog, TelegramDialog.id == TelegramMessage.dialog_id)
                    .where(
                        TelegramMessage.tenant_id == job.tenant_id,
                        TelegramMessage.id.in_(message_ids),
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                        TelegramDialog.classification != "automated_account",
                    )
                    .order_by(TelegramMessage.dialog_id, TelegramMessage.telegram_message_id)
                )
            )
            signal_ids: list[str] = []
            for message in messages:
                previous = list(
                    await session.scalars(
                        select(TelegramMessage)
                        .where(
                            TelegramMessage.dialog_id == message.dialog_id,
                            TelegramMessage.telegram_message_id < message.telegram_message_id,
                        )
                        .order_by(TelegramMessage.telegram_message_id.desc())
                        .limit(10)
                    )
                )
                previous.reverse()
                created = await self.scan_message(session, message, previous)
                signal_ids.extend(item.id for item in created)
            return signal_ids

        signal_ids = await self.transactions.run(write)
        await self._schedule_temporal_jobs(message_ids)
        async with self.session_factory() as session:
            signals = list(await session.scalars(select(Signal).where(Signal.id.in_(signal_ids))))
        jobs = await self.enqueue_triage(signals)
        return {"signals": len(signal_ids), "triage_jobs": len(jobs)}

    async def scan_message(
        self,
        session: AsyncSession,
        message: TelegramMessage,
        previous: list[TelegramMessage],
    ) -> list[Signal]:
        settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == message.tenant_id)
        )
        dialog = await session.get(TelegramDialog, message.dialog_id)
        relevance = classify_message_relevance(
            message.body_text,
            dialog_classification=dialog.classification if dialog else None,
        )
        if not relevance.business_relevant:
            await self._clear_non_business_state(session, message)
            return []
        candidates = self.engine.scan(
            message,
            previous,
            response_sla_minutes=settings.response_sla_minutes,
            timezone=settings.timezone,
        )
        if not candidates:
            await self._update_dialog_state(
                session, message, [], None, settings.response_sla_minutes
            )
            return []
        employee_id = await self._employee_id(session, message)
        created: list[Signal] = []
        for candidate in candidates:
            logical_source = (
                f"peer:{dialog.canonical_peer_id}:{message.telegram_message_id}"
                if dialog and dialog.dialog_type == "group"
                else f"message:{message.id}"
            )
            fingerprint = hashlib.sha256(
                f"{message.tenant_id}:{logical_source}:{candidate.signal_type}".encode()
            ).hexdigest()
            exists = await session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
            provenance = {
                "connection_id": message.connection_id,
                "dialog_id": message.dialog_id,
                "message_id": message.id,
                "telegram_message_id": message.telegram_message_id,
            }
            if exists is not None:
                metadata = dict(exists.metadata_json or {})
                sources = list(metadata.get("source_provenance") or [])
                if not any(
                    item.get("connection_id") == message.connection_id
                    and item.get("telegram_message_id") == message.telegram_message_id
                    for item in sources
                ):
                    sources.append(provenance)
                exists.local_score = max(exists.local_score, candidate.score)
                exists.criticality = max(exists.criticality, candidate.score)
                exists.reason = candidate.reason
                exists.metadata_json = {
                    **metadata,
                    "features": candidate.features,
                    "source_provenance": sources,
                    "re_evaluated_at": datetime.now(UTC).isoformat(),
                }
                created.append(exists)
                continue
            fast_lane = self._fast_lane_match(
                settings.critical_fast_lane_rules,
                message,
                candidate.signal_type,
            )
            if (
                fast_lane is None
                and candidate.signal_type == "invoice_received"
                and candidate.score >= 80
            ):
                fast_lane = {
                    "id": "builtin-invoice-metadata",
                    "criticality": max(90, candidate.score),
                }
            criticality = max(
                candidate.score,
                int(fast_lane["criticality"]) if fast_lane else candidate.score,
            )
            signal = Signal(
                tenant_id=message.tenant_id,
                telegram_connection_id=message.connection_id,
                dialog_id=message.dialog_id,
                source_message_id=message.id,
                employee_id=employee_id,
                fingerprint=fingerprint,
                signal_type=candidate.signal_type,
                local_score=candidate.score,
                criticality=criticality,
                reason=candidate.reason,
                detected_at=message.sent_at,
                metadata_json={
                    "features": candidate.features,
                    "source_provenance": [provenance],
                    **(
                        {
                            "fast_lane": {
                                "rule_id": fast_lane["id"],
                                "provisional": True,
                            }
                        }
                        if fast_lane
                        else {}
                    ),
                },
            )
            session.add(signal)
            await session.flush()
            if candidate.signal_type == "employee_commitment":
                await self._create_commitment(session, signal, message, candidate, employee_id)
            created.append(signal)
        await self._update_dialog_state(
            session, message, candidates, employee_id, settings.response_sla_minutes
        )
        return created

    @staticmethod
    async def _clear_non_business_state(session: AsyncSession, message: TelegramMessage) -> None:
        state = await session.scalar(
            select(DialogState).where(DialogState.dialog_id == message.dialog_id)
        )
        if state is None or state.response_expected_message_id != message.id:
            return
        state.awaiting_employee_since = None
        state.response_expected_message_id = None
        state.next_sla_check_at = None

    async def _schedule_temporal_jobs(self, message_ids: list[str]) -> None:
        async with self.session_factory() as session:
            messages = list(
                await session.scalars(
                    select(TelegramMessage).where(TelegramMessage.id.in_(message_ids))
                )
            )
            states = {
                item.dialog_id: item
                for item in await session.scalars(
                    select(DialogState).where(
                        DialogState.dialog_id.in_({item.dialog_id for item in messages})
                    )
                )
            }
            commitments = list(
                await session.scalars(
                    select(Commitment).where(Commitment.source_message_id.in_(message_ids))
                )
            )
        for message in messages:
            state = states.get(message.dialog_id)
            if (
                state
                and state.response_expected_message_id == message.id
                and state.next_sla_check_at is not None
            ):
                await self.queue.enqueue(
                    "dialog.sla_check",
                    {"dialog_id": message.dialog_id, "expected_message_id": message.id},
                    tenant_id=message.tenant_id,
                    telegram_account_id=message.connection_id,
                    dialog_id=message.dialog_id,
                    scheduled_at=state.next_sla_check_at,
                    idempotency_key=f"dialog-sla:{message.dialog_id}:{message.id}",
                    category="reconciliation",
                    cost_class="light",
                )
        for commitment in commitments:
            if commitment.deadline_at is not None:
                await self.queue.enqueue(
                    "commitment.deadline_check",
                    {"commitment_id": commitment.id},
                    tenant_id=commitment.tenant_id,
                    telegram_account_id=commitment.connection_id,
                    dialog_id=commitment.dialog_id,
                    scheduled_at=commitment.deadline_at,
                    idempotency_key=f"commitment-deadline:{commitment.id}",
                    category="reconciliation",
                    cost_class="light",
                )

    async def enqueue_triage(self, signals: list[Signal]) -> list[str]:
        source_ids = {signal.source_message_id for signal in signals}
        async with self.session_factory() as session:
            source_messages = {
                item.id: item
                for item in await session.scalars(
                    select(TelegramMessage).where(TelegramMessage.id.in_(source_ids))
                )
            }
        job_ids: list[str] = []
        for signal in signals:
            settings, threshold = await self._triage_policy(signal)
            if (signal.metadata_json or {}).get("fast_lane"):
                await self.notifications.plan_for_signal(signal.id)
            if signal.local_score < threshold:
                continue
            priority = (
                JOB_PRIORITY["P0"]
                if signal.local_score >= settings.signal_immediate_threshold
                else JOB_PRIORITY["P1"]
                if signal.local_score >= settings.signal_problem_threshold
                else JOB_PRIORITY["P2"]
            )
            source = source_messages.get(signal.source_message_id)
            source_version = self._triage_source_version(source)
            job_ids.append(
                await self.queue.enqueue(
                    "signal.ai_triage",
                    {"signal_id": signal.id, "source_version": source_version},
                    tenant_id=signal.tenant_id,
                    telegram_account_id=signal.telegram_connection_id,
                    dialog_id=signal.dialog_id,
                    priority=priority,
                    idempotency_key=f"signal-ai-triage:{signal.id}:{source_version}",
                    correlation_id=signal.id,
                    is_heavy=False,
                    category="ai_fast",
                    cost_class="ai_fast",
                    max_attempts=3,
                )
            )
        return job_ids

    @staticmethod
    def _triage_source_version(message: TelegramMessage | None) -> str:
        if message is None:
            return "source-missing"
        payload = {
            "edited_at": message.edited_at.isoformat() if message.edited_at else None,
            "body_text": message.body_text,
            "attachments": message.attachments_json,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:20]

    @staticmethod
    def _fast_lane_match(
        rules: list[dict[str, object]], message: TelegramMessage, signal_type: str
    ) -> dict[str, object] | None:
        text = message.body_text or ""
        lowered = text.lower()
        attachment_names = [
            str(item.get("name") or "").lower() for item in message.attachments_json
        ]
        mime_types = {str(item.get("mime_type") or "").lower() for item in message.attachments_json}
        extensions = {name.rsplit(".", 1)[-1] for name in attachment_names if "." in name}
        for rule in rules or []:
            if not bool(rule.get("enabled", True)):
                continue
            signal_types = {str(item) for item in rule.get("signal_types", [])}
            if signal_types and signal_type not in signal_types:
                continue
            any_terms = [str(item).lower() for item in rule.get("contains_any", [])]
            all_terms = [str(item).lower() for item in rule.get("contains_all", [])]
            if any_terms and not any(term in lowered for term in any_terms):
                continue
            if all_terms and not all(term in lowered for term in all_terms):
                continue
            name_terms = [str(item).lower() for item in rule.get("attachment_name_any", [])]
            if name_terms and not any(
                term in name for term in name_terms for name in attachment_names
            ):
                continue
            allowed_mimes = {str(item).lower() for item in rule.get("mime_types", [])}
            if allowed_mimes and not mime_types.intersection(allowed_mimes):
                continue
            allowed_extensions = {
                str(item).lower().lstrip(".") for item in rule.get("extensions", [])
            }
            if allowed_extensions and not extensions.intersection(allowed_extensions):
                continue
            directions = {str(item) for item in rule.get("directions", [])}
            direction = "outgoing" if message.outgoing else "incoming"
            if directions and direction not in directions:
                continue
            sender_roles = {str(item) for item in rule.get("sender_roles", [])}
            if sender_roles and message.sender_role not in sender_roles:
                continue
            has_amount = any(char.isdigit() for char in text) and any(
                marker in lowered for marker in ("₽", "руб", "$", "€", "usd", "eur")
            )
            if bool(rule.get("requires_amount")) and not has_amount:
                continue
            if any_terms or all_terms or name_terms or allowed_mimes or allowed_extensions:
                return rule
        return None

    async def _triage_policy(self, signal: Signal) -> tuple[TenantSettings, int]:
        async with self.session_factory() as session:
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == signal.tenant_id)
            )
            threshold = settings.signal_report_threshold
            if settings.ai_daily_soft_limit:
                used = int(
                    await session.scalar(
                        select(
                            func.coalesce(
                                func.sum(AIUsageCall.input_tokens + AIUsageCall.output_tokens), 0
                            )
                        ).where(
                            AIUsageCall.tenant_id == signal.tenant_id,
                            AIUsageCall.occurred_at
                            >= datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC),
                        )
                    )
                )
                if used >= settings.ai_daily_soft_limit:
                    threshold = settings.signal_problem_threshold
            return settings, threshold

    @staticmethod
    async def _employee_id(session: AsyncSession, message: TelegramMessage) -> str | None:
        if message.outgoing:
            return await session.scalar(
                select(TelegramConnection.assigned_employee_id).where(
                    TelegramConnection.id == message.connection_id
                )
            )
        if message.sender_id is None:
            return None
        direct = await session.scalar(
            select(Employee.id).where(
                Employee.tenant_id == message.tenant_id,
                Employee.telegram_user_id == message.sender_id,
            )
        )
        if direct:
            return direct
        mapped = await session.scalar(
            select(EmployeeTelegramAccount.employee_id).where(
                EmployeeTelegramAccount.tenant_id == message.tenant_id,
                EmployeeTelegramAccount.telegram_user_id == message.sender_id,
            )
        )
        if mapped:
            return mapped
        # Incoming client messages belong to the employee assigned to the
        # observed work account unless a more precise sender mapping exists.
        return await session.scalar(
            select(TelegramConnection.assigned_employee_id).where(
                TelegramConnection.id == message.connection_id,
                TelegramConnection.tenant_id == message.tenant_id,
            )
        )

    @staticmethod
    async def _create_commitment(
        session: AsyncSession,
        signal: Signal,
        message: TelegramMessage,
        candidate: LocalSignalCandidate,
        employee_id: str | None,
    ) -> None:
        fingerprint = hashlib.sha256(f"commitment:{signal.fingerprint}".encode()).hexdigest()
        if await session.scalar(select(Commitment.id).where(Commitment.fingerprint == fingerprint)):
            return
        session.add(
            Commitment(
                tenant_id=message.tenant_id,
                connection_id=message.connection_id,
                dialog_id=message.dialog_id,
                source_message_id=message.id,
                signal_id=signal.id,
                responsible_employee_id=employee_id,
                fingerprint=fingerprint,
                commitment_type="employee_promise",
                expected_action=(message.body_text or "Обещанное действие")[:2000],
                deadline_at=candidate.commitment_deadline,
                confidence=min(0.95, candidate.score / 100),
                metadata_json={"local_features": candidate.features},
            )
        )

    @staticmethod
    async def _update_dialog_state(
        session: AsyncSession,
        message: TelegramMessage,
        candidates: list[LocalSignalCandidate],
        employee_id: str | None,
        response_sla_minutes: int,
    ) -> None:
        state = await session.scalar(
            select(DialogState).where(DialogState.dialog_id == message.dialog_id)
        )
        if state is None:
            state = DialogState(
                tenant_id=message.tenant_id,
                connection_id=message.connection_id,
                dialog_id=message.dialog_id,
                open_commitments_json=[],
                unresolved_questions_json=[],
            )
            session.add(state)
        elif state.last_activity_at is not None and SignalService._aware(
            state.last_activity_at
        ) > SignalService._aware(message.sent_at):
            # Historical scan batches may complete out of order. An older message must
            # never reopen an SLA timer already cleared by a newer employee reply.
            return
        state.last_activity_at = message.sent_at
        employee_side = message.outgoing or message.sender_role in {"employee", "account_owner"}
        if employee_side:
            state.last_employee_message_id = message.telegram_message_id
            state.last_employee_reply_at = message.sent_at
            state.awaiting_employee_since = None
            state.response_expected_message_id = None
            state.next_sla_check_at = None
            state.awaiting_customer_since = message.sent_at
        else:
            state.last_customer_message_id = message.telegram_message_id
            state.last_customer_message_at = message.sent_at
            state.awaiting_customer_since = None
            state.awaiting_employee_since = message.sent_at
            state.response_expected_message_id = message.id
            state.next_sla_check_at = message.sent_at + timedelta(minutes=response_sla_minutes)
        meaningful = False
        if not message.outgoing and "?" in (message.body_text or ""):
            questions = list(state.unresolved_questions_json or [])
            questions.append(
                {"message_id": message.telegram_message_id, "sent_at": message.sent_at.isoformat()}
            )
            state.unresolved_questions_json = questions[-20:]
            meaningful = True
        commitments = [item for item in candidates if item.signal_type == "employee_commitment"]
        if commitments:
            current = list(state.open_commitments_json or [])
            current.extend(
                {
                    "message_id": message.telegram_message_id,
                    "employee_id": employee_id,
                    "deadline": item.commitment_deadline.isoformat()
                    if item.commitment_deadline
                    else None,
                }
                for item in commitments
            )
            state.open_commitments_json = current[-20:]
            meaningful = True
        if meaningful or any(item.score >= 65 for item in candidates):
            state.meaningful_version = (state.meaningful_version or 0) + 1

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
