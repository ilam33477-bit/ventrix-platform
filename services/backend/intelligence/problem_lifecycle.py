from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.problems import ALLOWED_TRANSITIONS, ProblemStatus

from ..database import SQLiteTransactionManager
from ..models import Employee, OperationalProblem, ProblemTransition

TERMINAL_PROBLEM_STATUSES = frozenset(
    {
        ProblemStatus.RESOLVED.value,
        ProblemStatus.AUTO_RESOLVED.value,
        ProblemStatus.FALSE_POSITIVE.value,
        ProblemStatus.IGNORED.value,
    }
)
ACTIVE_PROBLEM_STATUSES = frozenset(status.value for status in ProblemStatus) - {
    *TERMINAL_PROBLEM_STATUSES,
}


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    target: ProblemStatus
    actor_type: str
    actor_id: str | None
    reason: str
    evidence: str | None = None
    responsible_employee_id: str | None = None
    deadline_at: datetime | None = None


class ProblemLifecycleService:
    """Persistent adapter around the dependency-light domain FSM."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)

    async def transition(
        self,
        tenant_id: str,
        problem_id: str,
        request: TransitionRequest,
    ) -> OperationalProblem:
        reason = request.reason.strip()
        evidence = (request.evidence or "").strip() or None
        if not reason:
            raise ValueError("problem transition reason is required")

        async def write(session: AsyncSession) -> str:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == tenant_id,
                )
            )
            if problem is None:
                raise LookupError("problem not found in tenant")
            try:
                current = ProblemStatus(problem.status)
            except ValueError as exc:
                raise ValueError(f"unknown current problem status: {problem.status}") from exc
            if request.target not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(
                    f"illegal problem transition: {current.value} -> {request.target.value}"
                )

            responsible_id = request.responsible_employee_id or problem.responsible_employee_id
            if request.target == ProblemStatus.ASSIGNED:
                if responsible_id is None:
                    raise ValueError("assignment requires responsible_employee_id")
                employee = await session.scalar(
                    select(Employee.id).where(
                        Employee.id == responsible_id,
                        Employee.tenant_id == tenant_id,
                        Employee.status == "active",
                    )
                )
                if employee is None:
                    raise ValueError("responsible employee is not active in tenant")
                problem.responsible_employee_id = responsible_id

            if request.target in {ProblemStatus.RESOLVED, ProblemStatus.AUTO_RESOLVED}:
                if request.target == ProblemStatus.AUTO_RESOLVED and evidence is None:
                    raise ValueError("automatic resolution requires verification evidence")
                problem.resolved_at = datetime.now(UTC)
                problem.closed_reason = reason
                problem.resolution_evidence = evidence
            elif request.target == ProblemStatus.REOPENED:
                problem.reopened_at = datetime.now(UTC)
                problem.resolved_at = None
                problem.closed_reason = None
                problem.resolution_evidence = None

            if request.deadline_at is not None:
                problem.deadline_at = request.deadline_at
            previous = problem.status
            problem.status = request.target.value
            session.add(
                ProblemTransition(
                    tenant_id=tenant_id,
                    problem_id=problem.id,
                    from_status=previous,
                    to_status=problem.status,
                    actor_type=request.actor_type,
                    actor_id=request.actor_id,
                    reason=reason,
                    evidence=evidence,
                    occurred_at=datetime.now(UTC),
                )
            )
            return problem.id

        persisted_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            persisted = await session.get(OperationalProblem, persisted_id)
            if persisted is None:  # pragma: no cover - transaction just persisted it
                raise LookupError("problem disappeared after transition")
            return persisted

    async def advance_for_automatic_resolution(
        self,
        tenant_id: str,
        problem_id: str,
        *,
        evidence: str,
        reason: str,
    ) -> OperationalProblem | None:
        """Close only problems that already have an owner and can enter active work.

        A verifier finding does not silently close an unconfirmed/unassigned problem.
        It remains available as verification evidence for a human decision.
        """
        async with self.session_factory() as session:
            problem = await session.scalar(
                select(OperationalProblem).where(
                    OperationalProblem.id == problem_id,
                    OperationalProblem.tenant_id == tenant_id,
                )
            )
        if problem is None or problem.status in TERMINAL_PROBLEM_STATUSES:
            return problem
        actor = None
        if problem.status == ProblemStatus.ASSIGNED.value:
            await self.transition(
                tenant_id,
                problem_id,
                TransitionRequest(
                    ProblemStatus.IN_PROGRESS,
                    "system",
                    actor,
                    "Автоматическая проверка начала remediation.",
                ),
            )
        elif problem.status == ProblemStatus.WAITING.value:
            await self.transition(
                tenant_id,
                problem_id,
                TransitionRequest(
                    ProblemStatus.IN_PROGRESS,
                    "system",
                    actor,
                    "Получено новое релевантное сообщение для проверки.",
                ),
            )
        elif problem.status != ProblemStatus.IN_PROGRESS.value:
            return None
        return await self.transition(
            tenant_id,
            problem_id,
            TransitionRequest(
                ProblemStatus.AUTO_RESOLVED,
                "system",
                actor,
                reason,
                evidence=evidence,
            ),
        )


def initialize_problem_lifecycle(
    problem: OperationalProblem,
    *,
    responsible_employee_id: str | None,
    requires_confirmation: bool,
    reason: str,
) -> list[ProblemTransition]:
    """Initialize a newly flushed problem without skipping its audit trail."""
    now = datetime.now(UTC)
    transitions: list[ProblemTransition] = []

    def append(previous: ProblemStatus, target: ProblemStatus, transition_reason: str) -> None:
        transitions.append(
            ProblemTransition(
                tenant_id=problem.tenant_id,
                problem_id=problem.id,
                from_status=previous.value,
                to_status=target.value,
                actor_type="system",
                actor_id=None,
                reason=transition_reason,
                occurred_at=now,
            )
        )

    if requires_confirmation or responsible_employee_id is None:
        append(ProblemStatus.NEW, ProblemStatus.NEEDS_CONFIRMATION, reason)
        problem.status = ProblemStatus.NEEDS_CONFIRMATION.value
        return transitions
    append(ProblemStatus.NEW, ProblemStatus.ACKNOWLEDGED, "Сигнал подтверждён анализатором.")
    append(ProblemStatus.ACKNOWLEDGED, ProblemStatus.ASSIGNED, reason)
    problem.status = ProblemStatus.ASSIGNED.value
    problem.responsible_employee_id = responsible_employee_id
    return transitions
