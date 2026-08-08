import unittest
from uuid import uuid4

from packages.ops_core import (
    AIModelRouter,
    AnalysisContext,
    Problem,
    ProblemPriority,
    ProblemStatus,
    RouteName,
)


class RouterTests(unittest.TestCase):
    def test_simple_task_uses_flash_without_thinking(self) -> None:
        route = AIModelRouter().choose(AnalysisContext(task_type="promise_extraction"))
        self.assertEqual(route.name, RouteName.FAST)
        self.assertEqual(route.model, "deepseek-v4-flash")
        self.assertFalse(route.thinking)

    def test_critical_task_uses_pro_max(self) -> None:
        route = AIModelRouter().choose(
            AnalysisContext(task_type="critical_recheck", priority="critical")
        )
        self.assertEqual(route.name, RouteName.CRITICAL)
        self.assertEqual(route.model, "deepseek-v4-pro")
        self.assertEqual(route.reasoning_effort, "max")

    def test_budget_pressure_keeps_complex_case_on_flash(self) -> None:
        route = AIModelRouter().choose(
            AnalysisContext(
                task_type="complaint_detection",
                initial_confidence=0.4,
                budget_pressure=True,
            )
        )
        self.assertEqual(route.name, RouteName.BALANCED)
        self.assertEqual(route.model, "deepseek-v4-flash")


class ProblemTests(unittest.TestCase):
    def test_happy_path_preserves_transition_history(self) -> None:
        actor = uuid4()
        problem = Problem(uuid4(), "overdue_promise", ProblemPriority.HIGH, 0.91)
        for target in (
            ProblemStatus.ACKNOWLEDGED,
            ProblemStatus.ASSIGNED,
            ProblemStatus.IN_PROGRESS,
            ProblemStatus.RESOLVED,
        ):
            problem.transition(target, actor, "supported by evidence")
        self.assertEqual(problem.status, ProblemStatus.RESOLVED)
        self.assertEqual(len(problem.transitions), 4)

    def test_illegal_transition_is_rejected(self) -> None:
        problem = Problem(uuid4(), "complaint", ProblemPriority.CRITICAL, 0.95)
        with self.assertRaises(ValueError):
            problem.transition(ProblemStatus.RESOLVED, uuid4(), "too early")


if __name__ == "__main__":
    unittest.main()
