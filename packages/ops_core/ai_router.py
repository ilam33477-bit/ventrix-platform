from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar


class RouteName(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Route:
    name: RouteName
    model: str
    thinking: bool
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    task_type: str
    message_count: int = 1
    context_chars: int = 0
    participants: int = 2
    contradiction: bool = False
    initial_confidence: float | None = None
    priority: str = "medium"
    potential_amount: int | None = None
    high_value_threshold: int = 500_000
    requires_deep_analysis: bool = False
    historical_false_positive_rate: float = 0.0
    remaining_pro_runs: int = 1
    budget_pressure: bool = False
    is_recheck: bool = False


@dataclass(frozen=True, slots=True)
class RouterPolicy:
    fast_model: str = "deepseek-v4-flash"
    deep_model: str = "deepseek-v4-pro"
    low_confidence: float = 0.72
    max_fast_messages: int = 80
    max_fast_context_chars: int = 32_000
    high_false_positive_rate: float = 0.2


class AIModelRouter:
    """Pure routing policy. Provider calls and budget reservations live outside it."""

    _critical_tasks: ClassVar[set[str]] = {
        "owner_weekly_report",
        "critical_recheck",
        "management_recommendation",
    }
    _simple_tasks: ClassVar[set[str]] = {
        "chat_classification",
        "dialogue_summary",
        "promise_extraction",
        "deadline_extraction",
        "complaint_detection",
        "meeting_detection",
        "unanswered_detection",
        "followup_detection",
        "remediation_check",
    }

    def __init__(self, policy: RouterPolicy | None = None) -> None:
        self.policy = policy or RouterPolicy()

    def choose(self, context: AnalysisContext) -> Route:
        p = self.policy
        high_value = (
            context.potential_amount is not None
            and context.potential_amount >= context.high_value_threshold
        )
        critical = context.priority == "critical" or context.task_type in self._critical_tasks
        complex_case = any(
            (
                context.contradiction,
                context.requires_deep_analysis,
                high_value,
                context.participants > 4,
                context.message_count > p.max_fast_messages,
                context.context_chars > p.max_fast_context_chars,
                context.initial_confidence is not None
                and context.initial_confidence < p.low_confidence,
                context.historical_false_positive_rate >= p.high_false_positive_rate,
            )
        )

        if critical and context.remaining_pro_runs > 0:
            return Route(RouteName.CRITICAL, p.deep_model, True, "max")
        if complex_case and context.remaining_pro_runs > 0 and not context.budget_pressure:
            return Route(RouteName.DEEP, p.deep_model, True, "high")
        if complex_case or context.is_recheck:
            return Route(RouteName.BALANCED, p.fast_model, True, "high")
        return Route(RouteName.FAST, p.fast_model, False, None)

    def requires_second_pass(self, context: AnalysisContext) -> bool:
        return any(
            (
                context.requires_deep_analysis,
                context.contradiction,
                context.priority in {"high", "critical"},
                context.initial_confidence is not None
                and context.initial_confidence < self.policy.low_confidence,
                context.potential_amount is not None
                and context.potential_amount >= context.high_value_threshold,
                context.historical_false_positive_rate >= self.policy.high_false_positive_rate,
            )
        )
