from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx

from ..config import get_settings

REQUIRED_COMPONENTS = {
    "api",
    "scheduler",
    "owner_bot",
    "client_bots",
    "telegram_runtime",
}


def validate_production_state(
    *,
    unauthenticated_metrics_status: int,
    metrics: dict[str, Any],
    details: dict[str, Any],
    expected_revision: str,
) -> dict[str, Any]:
    if unauthenticated_metrics_status not in {401, 403}:
        raise RuntimeError("metrics endpoint is not protected")
    if details.get("release_revision") != expected_revision:
        raise RuntimeError("running release revision does not match expected revision")

    components = list((metrics.get("runtime") or {}).get("components") or [])
    by_name = {str(item.get("component")): item for item in components}
    missing = sorted(REQUIRED_COMPONENTS - set(by_name))
    if not any(name.startswith("worker:") for name in by_name):
        missing.append("worker:*")
    if missing:
        raise RuntimeError("runtime heartbeat is missing for: " + ", ".join(missing))
    unhealthy = sorted(
        name
        for name, item in by_name.items()
        if name != "platform_monitor" and item.get("status") != "healthy"
    )
    if unhealthy:
        raise RuntimeError("runtime heartbeat is unhealthy for: " + ", ".join(unhealthy))

    return {
        "release_revision": expected_revision,
        "runtime_components": len(components),
        "queue_depth": int((metrics.get("queue") or {}).get("depth") or 0),
        "overdue_reports": int((metrics.get("reports") or {}).get("overdue") or 0),
        "disk_free_percent": (metrics.get("host") or {}).get("disk_free_percent"),
    }


def verify_once(base_url: str, owner_token: str, expected_revision: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=10) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        unauthenticated_metrics = client.get("/metrics")
        headers = {"X-Owner-Token": owner_token}
        metrics = client.get("/metrics", headers=headers)
        details = client.get("/health/details", headers=headers)
    live.raise_for_status()
    ready.raise_for_status()
    metrics.raise_for_status()
    details.raise_for_status()
    return validate_production_state(
        unauthenticated_metrics_status=unauthenticated_metrics.status_code,
        metrics=metrics.json(),
        details=details.json(),
        expected_revision=expected_revision,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a running Ventrix release")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(max(1, args.attempts)):
        try:
            summary = verify_once(
                args.base_url,
                settings.owner_api_token.get_secret_value(),
                args.expected_revision,
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < max(1, args.attempts):
                time.sleep(max(0.1, args.interval))
    raise SystemExit(f"production verification failed: {last_error}")


if __name__ == "__main__":
    main()
