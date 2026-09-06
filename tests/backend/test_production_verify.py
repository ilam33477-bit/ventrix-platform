from __future__ import annotations

import pytest

from services.backend.scripts.verify_production import validate_production_state


def runtime_metrics(*, worker_status: str = "healthy") -> dict[str, object]:
    names = ["api", "scheduler", "owner_bot", "client_bots", "telegram_runtime"]
    components = [{"component": name, "status": "healthy"} for name in names]
    components.extend(
        [
            {
                "component": "worker:retired",
                "status": "stale",
                "heartbeat_age_seconds": 900,
            },
            {
                "component": "worker",
                "status": worker_status,
                "heartbeat_age_seconds": 1,
            },
        ]
    )
    return {
        "runtime": {"components": components},
        "queue": {"depth": 2},
        "reports": {"overdue": 1},
        "host": {"disk_free_percent": 25.0},
    }


def test_production_state_requires_protected_metrics_and_all_heartbeats() -> None:
    summary = validate_production_state(
        unauthenticated_metrics_status=401,
        metrics=runtime_metrics(),
        details={"release_revision": "abc123"},
        expected_revision="abc123",
    )
    assert summary == {
        "release_revision": "abc123",
        "runtime_components": 7,
        "queue_depth": 2,
        "overdue_reports": 1,
        "disk_free_percent": 25.0,
    }


@pytest.mark.parametrize(
    ("status", "metrics", "details"),
    [
        (200, runtime_metrics(), {"release_revision": "abc123"}),
        (401, {"runtime": {"components": []}}, {"release_revision": "abc123"}),
        (401, runtime_metrics(worker_status="stale"), {"release_revision": "abc123"}),
        (401, runtime_metrics(), {"release_revision": "old"}),
    ],
)
def test_production_state_rejects_incomplete_or_old_release(
    status: int, metrics: dict[str, object], details: dict[str, str]
) -> None:
    with pytest.raises(RuntimeError):
        validate_production_state(
            unauthenticated_metrics_status=status,
            metrics=metrics,
            details=details,
            expected_revision="abc123",
        )
