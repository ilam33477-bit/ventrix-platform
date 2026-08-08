from __future__ import annotations

import hashlib
from datetime import UTC, datetime

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
    TelegramMessage,
    TenantSettings,
)
from .local_signals import LocalSignalCandidate, LocalSignalEngine


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

    async def local_scan_job(self, job: JobLease) -> dict[str, int]:
        message_id = str(job.payload["message_id"])

        async def write(session: AsyncSession) -> list[str]:
            message = await session.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.id == message_id,
                    TelegramMessage.tenant_id == job.tenant_id,
                )
            )
            if message is None:
                raise LookupError("message not found in tenant")
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
        candidates = self.engine.scan(
            message,
            previous,
            response_sla_minutes=settings.response_sla_minutes,
        )
        if not candidates:
            await self._update_dialog_state(session, message, [], None)
            return []
        employee_id = await self._employee_id(session, message)
        created: list[Signal] = []
        for candidate in candidates:
            fingerprint = hashlib.sha256(
                f"{message.tenant_id}:{message.id}:{candidate.signal_type}".encode()
            ).hexdigest()
            exists = await session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
            if exists is not None:
                continue
            signal = Signal(
                tenant_id=message.tenant_id,
                telegram_connection_id=message.connection_id,
                dialog_id=message.dialog_id,
                source_message_id=message.id,
                employee_id=employee_id,
                fingerprint=fingerprint,
                signal_type=candidate.signal_type,
                local_score=candidate.score,
                criticality=candidate.score,
                reason=candidate.reason,
                detected_at=message.sent_at,
                metadata_json={"features": candidate.features},
            )
            session.add(signal)
            await session.flush()
            if candidate.signal_type == "employee_commitment":
                await self._create_commitment(session, signal, message, candidate, employee_id)
            created.append(signal)
        await self._update_dialog_state(session, message, candidates, employee_id)
        return created

    async def enqueue_triage(self, signals: list[Signal]) -> list[str]:
        job_ids: list[str] = []
        for signal in signals:
            settings, threshold = await self._triage_policy(signal)
            if signal.local_score < threshold:
                continue
            priority = (
                JOB_PRIORITY["P0"]
                if signal.local_score >= settings.signal_immediate_threshold
                else JOB_PRIORITY["P1"]
                if signal.local_score >= settings.signal_problem_threshold
                else JOB_PRIORITY["P2"]
            )
            job_ids.append(
                await self.queue.enqueue(
                    "signal.ai_triage",
                    {"signal_id": signal.id},
                    tenant_id=signal.tenant_id,
                    telegram_account_id=signal.telegram_connection_id,
                    dialog_id=signal.dialog_id,
                    priority=priority,
                    idempotency_key=f"signal-ai-triage:{signal.id}",
                    correlation_id=signal.id,
                    is_heavy=False,
                    category="ai_fast",
                    cost_class="ai_fast",
                    max_attempts=3,
                )
            )
        return job_ids

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
        return await session.scalar(
            select(EmployeeTelegramAccount.employee_id).where(
                EmployeeTelegramAccount.tenant_id == message.tenant_id,
                EmployeeTelegramAccount.telegram_user_id == message.sender_id,
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
        state.last_activity_at = message.sent_at
        if message.outgoing:
            state.last_employee_message_id = message.telegram_message_id
        else:
            state.last_customer_message_id = message.telegram_message_id
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
            state.meaningful_version += 1
