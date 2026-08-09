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
    DialogState,
    OperationalProblem,
    ProblemVerification,
    Signal,
    TelegramConnection,
    TelegramMessage,
    TenantSettings,
)
from .notifications import NotificationOrchestrator
from .problem_lifecycle import (
    ACTIVE_PROBLEM_STATUSES,
    ProblemLifecycleService,
    initialize_problem_lifecycle,
)
from .remediation import RemediationDecision, RemediationVerifier

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
        verification_provider=None,
        verification_model: str = "deepseek-v4-flash",
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.notifications = NotificationOrchestrator(
            session_factory, queue, cooldown_minutes=notification_cooldown_minutes
        )
        self.lifecycle = ProblemLifecycleService(session_factory)
        self.verifier = RemediationVerifier(
            verification_provider,
            model=verification_model,
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
                        OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
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
            decision = await self._verify_problem(problem)
            if (
                decision.outcome == "fixed"
                and decision.confidence >= self.verifier.auto_close_confidence
            ):
                updated = await self.lifecycle.advance_for_automatic_resolution(
                    problem.tenant_id,
                    problem.id,
                    evidence=decision.reason,
                    reason="Remediation подтверждена последующими сообщениями.",
                )
                if updated is not None and updated.status == "auto_resolved":
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
        decision = await self._verify_problem(problem)
        resolved = False
        if (
            decision.outcome == "fixed"
            and decision.confidence >= self.verifier.auto_close_confidence
        ):
            updated = await self.lifecycle.advance_for_automatic_resolution(
                problem.tenant_id,
                problem.id,
                evidence=decision.reason,
                reason="Remediation подтверждена последующими сообщениями.",
            )
            resolved = updated is not None and updated.status == "auto_resolved"
        return {
            "problem_id": problem.id,
            "resolved": resolved,
            "verification": decision.outcome,
            "confidence": decision.confidence,
        }

    async def deadline_check(self, job: JobLease) -> dict[str, int]:
        """Targeted durable deadline timer; periodic reconciliation remains the recovery path."""
        return await self.reconcile(job)

    async def sla_check(self, job: JobLease) -> dict[str, object]:
        if job.tenant_id is None:
            raise ValueError("tenant is required")
        dialog_id = str(job.payload["dialog_id"])
        expected_message_id = str(job.payload["expected_message_id"])
        now = datetime.now(UTC)

        async def write(session: AsyncSession) -> tuple[str | None, str | None]:
            state = await session.scalar(
                select(DialogState).where(
                    DialogState.tenant_id == job.tenant_id,
                    DialogState.dialog_id == dialog_id,
                )
            )
            if (
                state is None
                or state.response_expected_message_id != expected_message_id
                or state.awaiting_employee_since is None
                or state.next_sla_check_at is None
                or self._aware(state.next_sla_check_at) > now
            ):
                return None, None
            source = await session.get(TelegramMessage, expected_message_id)
            if source is None:
                return None, None
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == job.tenant_id)
            )
            fingerprint = hashlib.sha256(
                f"sla:{job.tenant_id}:{dialog_id}:{expected_message_id}".encode()
            ).hexdigest()
            signal = await session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
            if signal is None:
                signal = Signal(
                    tenant_id=job.tenant_id,
                    telegram_connection_id=source.connection_id,
                    dialog_id=dialog_id,
                    source_message_id=source.id,
                    fingerprint=fingerprint,
                    signal_type="client_without_answer",
                    local_score=settings.signal_problem_threshold,
                    criticality=settings.signal_problem_threshold,
                    status="problem_created",
                    reason="Клиент не получил ответ в пределах SLA.",
                    detected_at=now,
                    processed_at=now,
                    metadata_json={"response_expected_message_id": expected_message_id},
                )
                session.add(signal)
                await session.flush()
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.fingerprint == f"sla-problem:{fingerprint}"
                )
            )
            if problem is None:
                responsible = await session.scalar(
                    select(TelegramConnection.assigned_employee_id).where(
                        TelegramConnection.id == source.connection_id
                    )
                )
                problem = OperationalProblem(
                    tenant_id=job.tenant_id,
                    connection_id=source.connection_id,
                    dialog_id=dialog_id,
                    source_message_id=source.id,
                    signal_id=signal.id,
                    responsible_employee_id=responsible,
                    fingerprint=f"sla-problem:{fingerprint}",
                    problem_type="client_without_answer",
                    priority="high",
                    confidence=1.0,
                    evidence=(source.body_text or "")[:2000],
                    explanation=signal.reason,
                    recommended_action="Ответить клиенту и подтвердить следующий шаг.",
                    occurred_at=now,
                    next_check_at=now,
                )
                session.add(problem)
                await session.flush()
                session.add_all(
                    initialize_problem_lifecycle(
                        problem,
                        responsible_employee_id=responsible,
                        requires_confirmation=responsible is None,
                        reason="SLA истёк без ответа сотрудника.",
                    )
                )
            return signal.id, problem.id

        signal_id, problem_id = await self.transactions.run(write)
        if signal_id and problem_id:
            await self.notifications.plan_for_signal(signal_id, problem_id)
        return {"created": bool(problem_id), "problem_id": problem_id}

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
                        OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                    )
                )
            )
            for problem in problems:
                problem.last_verified_at = now

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
                    responsible_employee_id=commitment.responsible_employee_id,
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
                session.add_all(
                    initialize_problem_lifecycle(
                        problem,
                        responsible_employee_id=commitment.responsible_employee_id,
                        requires_confirmation=commitment.responsible_employee_id is None,
                        reason="Ответственный унаследован из просроченного обязательства.",
                    )
                )
                created = True
            commitment.last_checked_at = now
            return signal.id, problem.id, created

        return await self.transactions.run(write)

    async def _verify_problem(self, problem: OperationalProblem) -> RemediationDecision:
        async with self.session_factory() as session:
            source = await session.get(TelegramMessage, problem.source_message_id)
            messages = list(
                await session.scalars(
                    select(TelegramMessage)
                    .where(
                        TelegramMessage.tenant_id == problem.tenant_id,
                        TelegramMessage.dialog_id == problem.dialog_id,
                        TelegramMessage.telegram_message_id > source.telegram_message_id,
                        TelegramMessage.deleted_at.is_(None),
                    )
                    .order_by(TelegramMessage.telegram_message_id.asc())
                    .limit(50)
                )
            )
        decision = await self.verifier.verify(problem, messages)
        await self._record_verification(problem, decision)
        return decision

    async def _record_verification(
        self, problem: OperationalProblem, decision: RemediationDecision
    ) -> None:
        async def write(session: AsyncSession) -> None:
            current = await session.get(OperationalProblem, problem.id)
            latest = await session.scalar(
                select(ProblemVerification)
                .where(ProblemVerification.problem_id == problem.id)
                .order_by(ProblemVerification.checked_at.desc())
                .limit(1)
            )
            evidence_ids = list(decision.evidence_message_ids)
            if (
                latest is not None
                and latest.outcome == decision.outcome
                and latest.evidence_message_ids_json == evidence_ids
            ):
                current.last_verified_at = datetime.now(UTC)
                return
            session.add(
                ProblemVerification(
                    tenant_id=problem.tenant_id,
                    problem_id=problem.id,
                    outcome=decision.outcome,
                    confidence=decision.confidence,
                    method=decision.method,
                    verifier_version=self.verifier.version,
                    reason=decision.reason,
                    evidence_message_ids_json=evidence_ids,
                    checked_at=datetime.now(UTC),
                )
            )
            current.last_verified_at = datetime.now(UTC)

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
