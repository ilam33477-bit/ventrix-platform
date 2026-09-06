from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import RuntimeHealth

logger = logging.getLogger(__name__)


def normalized_component(value: str) -> str:
    cleaned = "".join(char for char in value.strip() if char.isalnum() or char in "._:-")
    if len(cleaned) <= 64:
        return cleaned or "unknown"
    digest = hashlib.sha256(cleaned.encode()).hexdigest()[:10]
    return f"{cleaned[:53]}:{digest}"


async def record_runtime_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    component: str,
    *,
    status: str = "healthy",
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    heartbeat_at = now or datetime.now(UTC)
    component = normalized_component(component)
    async with session_factory() as session:
        statement = insert(RuntimeHealth).values(
            component=component,
            status=status,
            heartbeat_at=heartbeat_at,
            details_json=details or {},
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[RuntimeHealth.component],
                set_={
                    "status": status,
                    "heartbeat_at": heartbeat_at,
                    "details_json": details or {},
                    "updated_at": heartbeat_at,
                },
            )
        )
        await session.commit()


async def runtime_heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    component: str,
    *,
    interval_seconds: float,
    details: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
) -> None:
    def current_details() -> dict[str, Any]:
        return details() if callable(details) else dict(details or {})

    try:
        while True:
            try:
                await record_runtime_heartbeat(
                    session_factory,
                    component,
                    details=current_details(),
                )
            except asyncio.CancelledError:
                raise
            # Health reporting must never terminate the process it observes.
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "runtime_heartbeat_failed",
                    extra={
                        "safe_context": {
                            "component": normalized_component(component),
                            "error_type": type(exc).__name__,
                        }
                    },
                )
            await asyncio.sleep(interval_seconds)
    finally:
        try:
            await asyncio.shield(
                record_runtime_heartbeat(
                    session_factory,
                    component,
                    status="stopped",
                    details=current_details(),
                )
            )
        except Exception:
            logger.debug("Could not persist stopped runtime heartbeat", exc_info=True)
