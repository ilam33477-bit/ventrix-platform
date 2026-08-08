from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JobLease, SQLiteJobQueue
from ..models import (
    Commitment,
    OperationalProblem,
    Signal,
    TelegramConnection,
    TelegramMessage,
    TenantSettings,
)
from .notifications import NotificationOrchestrator

COMPLETION_RE = re.compile(
    r"\b(?:отправил[аи]?|готово|прикрепил[аи]?|сделано|выполнено)\b", re.IGNORECASE
)


class ReconciliationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
        *,
        notification_cooldown_minutes: int = 120,
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.notifications = NotificationOrchestrator(
            session_factory, queue, cooldown_minutes=notification_cooldown_minutes
        )

    async def reconcile(self, job: JobLease) -> dict[str, int]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            commitments = list(
                await session.scalars(
                    select(Commitment).where(
                        Commitment.tenant_id == job.tenant_id,
                        Commitment.status == "open",
                    )
                )
            )
            problems = list(
                await session.scalars(
                    select(OperationalProblem).where(
                        OperationalProblem.tenant_id == job.tenant_id,
                        OperationalProblem.status.in_(("open", "needs_confirmation")),
                    )
                )
            )
        created: list[tuple[str, str]] = []
        completed = 0
        resolved = 0
        for commitment in commitments:
            if await self._is_completed(commitment):
                await self._complete_commitment(commitment.id, now)
                completed += 1
                continue
            deadline = self._aware(commitment.deadline_at)
            if deadline is not None and deadline < now:
                signal_id, problem_id, was_created = await self._overdue(commitment.id, now)
                if was_created:
                    created.append((signal_id, problem_id))
        for problem in problems:
            if problem.problem_type in {
                "waiting_customer",
                "client_without_answer",
                "customer_question",
            } and await self._has_employee_response(problem):
                await self._resolve_problem(problem.id, now)
                resolved += 1
        for signal_id, problem_id in created:
            await self.notifications.plan_for_signal(signal_id, problem_id)
        await self._mark_connections(job.tenant_id, now)
        return {
            "overdue_created": len(created),
            "commitments_completed": completed,
            "problems_resolved": resolved,
        }

    async def evaluate_problem(self, job: JobLease) -> dict[str, object]:
        problem_id = str(job.payload["problem_id"])
        async with self.session_factory() as session:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == job.tenant_id,
                )
            )
        if problem is None:
            raise LookupError("problem not found in tenant")
        resolved = await self._has_employee_response(problem)
        if resolved:
            await self._resolve_problem(problem.id, datetime.now(UTC))
        return {"problem_id": problem.id, "resolved": resolved}

    async def _is_completed(self, commitment: Commitment) -> bool:
        async with self.session_factory() as session:
            source = await session.get(TelegramMessage, commitment.source_message_id)
            later = list(
                await session.scalars(
                    select(TelegramMessage).where(
                        TelegramMessage.dialog_id == commitment.dialog_id,
                        TelegramMessage.outgoing.is_(True),
                        TelegramMessage.telegram_message_id > source.telegram_message_id,
                        TelegramMessage.deleted_at.is_(None),
                    )
                )
            )
        return any(COMPLETION_RE.search(item.body_text or "") for item in later)

    async def _complete_commitment(self, commitment_id: str, now: datetime) -> None:
        async def write(session: AsyncSession) -> None:
            commitment = await session.get(Commitment, commitment_id)
            commitment.status = "completed"
            commitment.completed_at = now
            commitment.last_checked_at = now
            problems = list(
                await session.scalars(
                    select(OperationalProblem).where(
                        OperationalProblem.commitment_id == commitment.id,
                        OperationalProblem.status != "resolved",
                    )
                )
            )
            for problem in problems:
                problem.status = "resolved"
                problem.resolved_at = now

        await self.transactions.run(write)

    async def _overdue(self, commitment_id: str, now: datetime) -> tuple[str, str, bool]:
        async def write(session: AsyncSession) -> tuple[str, str, bool]:
            commitment = await session.get(Commitment, commitment_id)
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == commitment.tenant_id)
            )
            delay_minutes = max(
                1, int((now - self._aware(commitment.deadline_at)).total_seconds() // 60)
            )
            criticality = min(100, settings.signal_problem_threshold + delay_minutes // 15)
            fingerprint = hashlib.sha256(f"overdue:{commitment.fingerprint}".encode()).hexdigest()
            signal = await session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
            if signal is None:
                signal = Signal(
                    tenant_id=commitment.tenant_id,
                    telegram_connection_id=commitment.connection_id,
                    dialog_id=commitment.dialog_id,
                    source_message_id=commitment.source_message_id,
                    employee_id=commitment.responsible_employee_id,
                    fingerprint=fingerprint,
                    signal_type="overdue_commitment",
                    local_score=criticality,
                    ai_score=criticality,
                    criticality=criticality,
                    status="problem_created",
                    reason=f"обещание просрочено на {delay_minutes} мин.",
                    detected_at=now,
                    processed_at=now,
                    metadata_json={"commitment_id": commitment.id, "delay_minutes": delay_minutes},
                )
                session.add(signal)
                await session.flush()
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.commitment_id == commitment.id,
                )
            )
            created = False
            if problem is None:
                source = await session.get(TelegramMessage, commitment.source_message_id)
                problem = OperationalProblem(
                    tenant_id=commitment.tenant_id,
                    connection_id=commitment.connection_id,
                    dialog_id=commitment.dialog_id,
                    source_message_id=commitment.source_message_id,
                    signal_id=signal.id,
                    commitment_id=commitment.id,
                    fingerprint=f"commitment-problem:{commitment.fingerprint}",
                    problem_type="overdue_commitment",
                    priority="critical"
                    if criticality >= settings.signal_immediate_threshold
                    else "high",
                    confidence=commitment.confidence,
                    evidence=(source.body_text or "")[:2000],
                    explanation=signal.reason,
                    recommended_action="Связаться с клиентом и закрыть обещанное действие.",
                    occurred_at=now,
                )
                session.add(problem)
                await session.flush()
                created = True
            commitment.last_checked_at = now
            return signal.id, problem.id, created

        return await self.transactions.run(write)

    async def _has_employee_response(self, problem: OperationalProblem) -> bool:
        async with self.session_factory() as session:
            source = await session.get(TelegramMessage, problem.source_message_id)
            return bool(
                await session.scalar(
                    select(TelegramMessage.id).where(
                        TelegramMessage.dialog_id == problem.dialog_id,
                        TelegramMessage.outgoing.is_(True),
                        TelegramMessage.telegram_message_id > source.telegram_message_id,
                        TelegramMessage.deleted_at.is_(None),
                    )
                )
            )

    async def _resolve_problem(self, problem_id: str, now: datetime) -> None:
        async def write(session: AsyncSession) -> None:
            problem = await session.get(OperationalProblem, problem_id)
            problem.status = "resolved"
            problem.resolved_at = now

        await self.transactions.run(write)

    async def _mark_connections(self, tenant_id: str, now: datetime) -> None:
        async def write(session: AsyncSession) -> None:
            connections = list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                    )
                )
            )
            for connection in connections:
                connection.last_full_reconciliation_at = now

        await self.transactions.run(write)

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
