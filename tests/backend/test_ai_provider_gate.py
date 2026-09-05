from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from services.backend.jobs.queue import (
    AI_PROVIDER_COST_CLASSES,
    AI_PROVIDER_JOB_TYPES,
    SQLiteJobQueue,
)
from services.backend.models import BackgroundJob
from services.backend.services.ai_provider_gate import AIConcurrencyBusy, SharedAIProvider


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def generate_json(self, **_kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "{}", {"input_tokens": 1, "output_tokens": 1}


async def test_interactive_calls_share_one_database_backed_slot(session_factory) -> None:
    provider = BlockingProvider()
    first = SharedAIProvider(
        session_factory, provider, max_active_requests=1, heartbeat_seconds=0.01
    )
    second = SharedAIProvider(
        session_factory, provider, max_active_requests=1, heartbeat_seconds=0.01
    )
    running = asyncio.create_task(first.generate_json(user_id="tenant-a"))
    await provider.started.wait()

    queue = SQLiteJobQueue(
        session_factory,
        resource_limits={"ai": (AI_PROVIDER_COST_CLASSES, 1)},
        resource_job_types={"ai": AI_PROVIDER_JOB_TYPES},
    )
    queued_id = await queue.enqueue(
        "signal.ai_triage", {}, category="ai_fast", cost_class="ai_fast"
    )
    assert await queue.claim_next("worker-ai") is None

    with pytest.raises(AIConcurrencyBusy):
        await second.generate_json(user_id="tenant-b")

    provider.release.set()
    assert await running == ("{}", {"input_tokens": 1, "output_tokens": 1})
    async with session_factory() as session:
        leases = list(
            await session.scalars(
                select(BackgroundJob).where(BackgroundJob.job_type == "ai.interactive")
            )
        )
    assert provider.calls == 1
    assert len(leases) == 1 and leases[0].status == "completed"
    claimed = await queue.claim_next("worker-ai")
    assert claimed is not None and claimed.id == queued_id


async def test_worker_ai_lease_blocks_interactive_provider(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    await queue.enqueue(
        "signal.ai_triage", {}, category="ai_fast", cost_class="ai_fast"
    )
    lease = await queue.claim_next("worker-ai")
    assert lease is not None
    provider = BlockingProvider()
    gated = SharedAIProvider(session_factory, provider, max_active_requests=1)

    with pytest.raises(AIConcurrencyBusy):
        await gated.generate_json(user_id="tenant-b")

    assert provider.calls == 0
