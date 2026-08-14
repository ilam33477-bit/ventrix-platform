from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class ProblemStatus(StrEnum):
    NEW = "new"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ACKNOWLEDGED = "acknowledged"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    RESOLVED = "resolved"
    AUTO_RESOLVED = "auto_resolved"
    FALSE_POSITIVE = "false_positive"
    IGNORED = "ignored"
    REOPENED = "reopened"


class ProblemPriority(StrEnum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


ALLOWED_TRANSITIONS: dict[ProblemStatus, frozenset[ProblemStatus]] = {
    ProblemStatus.NEW: frozenset(
        {ProblemStatus.NEEDS_CONFIRMATION, ProblemStatus.ACKNOWLEDGED, ProblemStatus.IGNORED}
    ),
    ProblemStatus.NEEDS_CONFIRMATION: frozenset(
        {ProblemStatus.ACKNOWLEDGED, ProblemStatus.FALSE_POSITIVE}
    ),
    ProblemStatus.ACKNOWLEDGED: frozenset(
        {ProblemStatus.ASSIGNED, ProblemStatus.FALSE_POSITIVE, ProblemStatus.IGNORED}
    ),
    ProblemStatus.ASSIGNED: frozenset({ProblemStatus.IN_PROGRESS, ProblemStatus.FALSE_POSITIVE}),
    ProblemStatus.IN_PROGRESS: frozenset(
        {
            ProblemStatus.WAITING,
            ProblemStatus.RESOLVED,
            ProblemStatus.AUTO_RESOLVED,
            ProblemStatus.FALSE_POSITIVE,
        }
    ),
    ProblemStatus.WAITING: frozenset({ProblemStatus.IN_PROGRESS, ProblemStatus.FALSE_POSITIVE}),
    ProblemStatus.RESOLVED: frozenset({ProblemStatus.REOPENED}),
    ProblemStatus.AUTO_RESOLVED: frozenset({ProblemStatus.REOPENED}),
    ProblemStatus.REOPENED: frozenset({ProblemStatus.ASSIGNED, ProblemStatus.IN_PROGRESS}),
    ProblemStatus.FALSE_POSITIVE: frozenset({ProblemStatus.REOPENED}),
    ProblemStatus.IGNORED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Transition:
    from_status: ProblemStatus
    to_status: ProblemStatus
    actor_id: UUID
    reason: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class Problem:
    tenant_id: UUID
    event_type: str
    priority: ProblemPriority
    confidence: float
    id: UUID = field(default_factory=uuid4)
    status: ProblemStatus = ProblemStatus.NEW
    assignee_id: UUID | None = None
    transitions: list[Transition] = field(default_factory=list)

    def transition(self, target: ProblemStatus, actor_id: UUID, reason: str) -> None:
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"illegal problem transition: {self.status} -> {target}")
        if target in {ProblemStatus.RESOLVED, ProblemStatus.AUTO_RESOLVED} and not reason.strip():
            raise ValueError("resolution requires evidence or a human reason")
        previous = self.status
        self.status = target
        self.transitions.append(Transition(previous, target, actor_id, reason.strip()))
