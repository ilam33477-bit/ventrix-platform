from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from queue import Empty
from uuid import UUID

import pytest
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.api.deepseek import DeepSeekAPIError
from services.backend.bot.sqlite_storage import SQLiteFSMStorage
from services.backend.database import SQLiteTransactionManager, build_engine
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.jobs.worker import HANDLERS, BackgroundWorker
from services.backend.models import (
    BackgroundJob,
    FSMState,
    TelegramConnection,
    TelegramRuntimeLease,
)
from services.backend.scripts.backup_sqlite import backup_database, restore_database
from services.backend.telegram_sessions.leases import TelegramRuntimeLeaseStore


def _transaction_probe(database_url, started, entered, release) -> None:
    async def probe() -> None:
        engine = build_engine(database_url, 5000)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        manager = SQLiteTransactionManager(factory)
        started.put(True)

        async def operation(_session) -> None:
            entered.put(True)
            release.wait(timeout=5)

        try:
            await manager.run(operation)
        finally:
            await engine.dispose()

    asyncio.run(probe())


@pytest.mark.asyncio
async def test_sqlite_pragmas_are_enabled(session_factory) -> None:
    async with session_factory() as session:
        connection = await session.connection()
        foreign_keys = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
        synchronous = (await connection.exec_driver_sql("PRAGMA synchronous")).scalar()
        busy_timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1  # NORMAL
    assert busy_timeout == 5000


def test_write_transaction_reserves_sqlite_before_operation_across_processes(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'process-lock.db'}"
    context = multiprocessing.get_context("spawn")
    started = context.Queue()
    entered = context.Queue()
    release_first = context.Event()
    release_second = context.Event()
    first = context.Process(
        target=_transaction_probe,
        args=(database_url, started, entered, release_first),
    )
    second = context.Process(
        target=_transaction_probe,
        args=(database_url, started, entered, release_second),
    )

    first.start()
    assert started.get(timeout=5) is True
    assert entered.get(timeout=5) is True
    second.start()
    assert started.get(timeout=5) is True
    try:
        with pytest.raises(Empty):
            entered.get(timeout=0.25)
        release_first.set()
        assert entered.get(timeout=5) is True
    finally:
        release_first.set()
        release_second.set()
        first.join(timeout=5)
        second.join(timeout=5)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0


@pytest.mark.asyncio
async def test_fsm_persists_after_storage_recreation(session_factory) -> None:
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    first = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=1))
    await first.set_state(key, "TenantCreateStates:name")
    await first.set_data(key, {"name": "Axiom"})

    second = SQLiteFSMStorage(session_factory, ttl=timedelta(hours=1))
    assert await second.get_state(key) == "TenantCreateStates:name"
    assert await second.get_data(key) == {"name": "Axiom"}


@pytest.mark.asyncio
async def test_fsm_serializes_common_domain_values(session_factory) -> None:
    class Status(Enum):
        ACTIVE = "active"

    key = StorageKey(bot_id=4, chat_id=5, user_id=6)
    storage = SQLiteFSMStorage(session_factory)
    await storage.set_data(
        key,
        {
            "date": date(2027, 1, 31),
            "datetime": datetime(2027, 1, 31, 9, 30, tzinfo=UTC),
            "uuid": UUID("12345678-1234-5678-1234-567812345678"),
            "enum": Status.ACTIVE,
        },
    )

    assert await storage.get_data(key) == {
        "date": "2027-01-31",
        "datetime": "2027-01-31T09:30:00+00:00",
        "uuid": "12345678-1234-5678-1234-567812345678",
        "enum": "active",
    }


@pytest.mark.asyncio
async def test_expired_fsm_state_is_cleaned(session_factory) -> None:
    key = StorageKey(bot_id=10, chat_id=20, user_id=30)
    storage = SQLiteFSMStorage(session_factory)
    await storage.set_state(key, "expired")
    async with session_factory() as session:
        row = await session.scalar(select(FSMState))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await storage.get_state(key) is None
    assert await storage.cleanup_expired() == 1


@pytest.mark.asyncio
async def test_background_job_execution_and_idempotency(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    first = await queue.enqueue("system.echo", {"value": 7}, idempotency_key="echo-7")
    duplicate = await queue.enqueue("system.echo", {"value": 8}, idempotency_key="echo-7")
    assert first == duplicate
    worker = BackgroundWorker(queue, "test-worker", HANDLERS)
    assert await worker.run_once()
    job = await queue.get(first)
    assert job.status == "completed"
    assert job.result_json["echo"] == {"value": 7}


@pytest.mark.asyncio
async def test_background_job_retry_then_completion(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("system.fail_once", {}, max_attempts=3)
    worker = BackgroundWorker(queue, "test-worker", HANDLERS)
    assert await worker.run_once()
    job = await queue.get(job_id)
    assert job.status == "retry_scheduled"
    assert job.attempts == 1
    async with session_factory() as session:
        await session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(scheduled_at=datetime.now(UTC))
        )
        await session.commit()
    assert await worker.run_once()
    job = await queue.get(job_id)
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_worker_honors_provider_retry_after(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("test.rate_limited", {}, max_attempts=3)

    async def rate_limited(_lease):
        raise DeepSeekAPIError(429, retry_after_seconds=45)

    before = datetime.now(UTC)
    worker = BackgroundWorker(queue, "ai-worker", {"test.rate_limited": rate_limited})
    assert await worker.run_once()
    job = await queue.get(job_id)

    assert job.status == "retry_scheduled"
    assert job.attempts == 1
    scheduled_at = (
        job.scheduled_at.replace(tzinfo=UTC)
        if job.scheduled_at.tzinfo is None
        else job.scheduled_at
    )
    assert scheduled_at >= before + timedelta(seconds=44)


@pytest.mark.asyncio
async def test_worker_does_not_retry_permanent_provider_error(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("test.bad_credentials", {}, max_attempts=3)

    async def bad_credentials(_lease):
        raise DeepSeekAPIError(401)

    worker = BackgroundWorker(queue, "ai-worker", {"test.bad_credentials": bad_credentials})
    assert await worker.run_once()
    job = await queue.get(job_id)

    assert job.status == "failed"
    assert job.attempts == 1
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_stale_running_job_is_recovered(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("system.echo", {})
    lease = await queue.claim_next("dead-worker")
    assert lease is not None
    async with session_factory() as session:
        await session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await session.commit()
    assert await queue.recover_stale(timedelta(minutes=5)) == 1
    job = await queue.get(job_id)
    assert job.status == "retry_scheduled"
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_stale_interactive_ai_lease_is_failed_not_requeued(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        lease = BackgroundJob(
            job_type="ai.interactive",
            category="ai_interactive",
            cost_class="ai_fast",
            payload_json={},
            status="running",
            priority=0,
            scheduled_at=now,
            started_at=now,
            locked_at=now - timedelta(minutes=20),
            heartbeat_at=now - timedelta(minutes=20),
            locked_by="dead-api",
            max_attempts=3,
        )
        session.add(lease)
        await session.commit()
        lease_id = lease.id

    queue = SQLiteJobQueue(session_factory)
    assert await queue.recover_stale(timedelta(minutes=5)) == 1
    stored = await queue.get(lease_id)

    assert stored.status == "failed"
    assert stored.attempts == 1
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_limited_concurrent_writes(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    ids = await asyncio.gather(
        *(queue.enqueue("system.echo", {"index": index}) for index in range(20))
    )
    assert len(set(ids)) == 20


@pytest.mark.asyncio
async def test_multiple_sqlite_workers_claim_each_job_once(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    job_ids = await asyncio.gather(
        *(queue.enqueue("test.multi", {"index": index}) for index in range(24))
    )

    async def handler(lease):
        await asyncio.sleep(0.002)
        return {"worker": lease.locked_by}

    workers = [
        BackgroundWorker(queue, f"worker:{index}", {"test.multi": handler}, heartbeat_seconds=0.01)
        for index in range(4)
    ]

    async def drain(worker):
        while await worker.run_once():
            pass

    await asyncio.gather(*(drain(worker) for worker in workers))
    jobs = [await queue.get(job_id) for job_id in job_ids]
    assert all(job.status == "completed" for job in jobs)
    assert all(job.attempts == 0 for job in jobs)
    assert len({job.result_json["worker"] for job in jobs}) >= 2


@pytest.mark.asyncio
async def test_worker_pool_claims_only_its_categories(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    telegram_id = await queue.enqueue("telegram.fetch_updates", {}, category="telegram")
    ai_id = await queue.enqueue("signal.ai_triage", {}, category="ai_fast")
    notification_id = await queue.enqueue("notification.manager", {}, category="notification")

    ai = await queue.claim_next("ai-pool", allowed_categories=frozenset({"ai_fast"}))
    notification = await queue.claim_next(
        "notification-pool", allowed_categories=frozenset({"notification"})
    )
    telegram = await queue.claim_next("telegram-pool", allowed_categories=frozenset({"telegram"}))

    assert ai is not None and ai.id == ai_id
    assert notification is not None and notification.id == notification_id
    assert telegram is not None and telegram.id == telegram_id


@pytest.mark.asyncio
async def test_heavy_jobs_from_two_tenants_run_independently_and_fairly(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        first_tenant = await make_service(session).create_tenant(tenant_payload)
        second_payload = tenant_payload.model_copy(
            update={
                "name": "Second Pilot",
                "owner_telegram_username": "second_owner",
                "owner_telegram_user_id": 555000222,
            }
        )
        second_tenant = await make_service(session).create_tenant(second_payload)

    queue = SQLiteJobQueue(
        session_factory,
        max_active_tenant_jobs=2,
        tenant_max_active_heavy_jobs=1,
        category_limits={"analysis": 2},
    )
    first_a = await queue.enqueue(
        "analysis.pipeline",
        {},
        tenant_id=first_tenant.id,
        category="analysis",
    )
    second_a = await queue.enqueue(
        "analysis.pipeline",
        {},
        tenant_id=first_tenant.id,
        category="analysis",
    )
    first_b = await queue.enqueue(
        "analysis.pipeline",
        {},
        tenant_id=second_tenant.id,
        category="analysis",
    )

    claimed_a = await queue.claim_next(
        "analysis-a", allowed_categories=frozenset({"analysis"})
    )
    claimed_b = await queue.claim_next(
        "analysis-b", allowed_categories=frozenset({"analysis"})
    )

    assert claimed_a is not None and claimed_a.id == first_a
    assert claimed_b is not None and claimed_b.id == first_b
    assert claimed_a.tenant_id != claimed_b.tenant_id
    assert (
        await queue.claim_next("analysis-c", allowed_categories=frozenset({"analysis"}))
        is None
    )

    assert await queue.complete(claimed_a)
    next_for_first_tenant = await queue.claim_next(
        "analysis-c", allowed_categories=frozenset({"analysis"})
    )
    assert next_for_first_tenant is not None
    assert next_for_first_tenant.id == second_a


@pytest.mark.asyncio
async def test_fairness_keeps_second_tenant_visible_beyond_large_backlog(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        first = await make_service(session).create_tenant(tenant_payload)
        second = await make_service(session).create_tenant(
            tenant_payload.model_copy(update={
                "name": "Second Backlog Tenant",
                "owner_telegram_username": "backlog_owner",
                "owner_telegram_user_id": 555000333,
            })
        )

    queue = SQLiteJobQueue(session_factory, category_limits={"ai_fast": 2})
    first_ids = [
        await queue.enqueue(
            "signal.ai_triage", {"index": index}, tenant_id=first.id, category="ai_fast"
        )
        for index in range(125)
    ]
    second_id = await queue.enqueue(
        "signal.ai_triage", {}, tenant_id=second.id, category="ai_fast"
    )

    first_lease = await queue.claim_next("ai-1", allowed_categories=frozenset({"ai_fast"}))
    second_lease = await queue.claim_next("ai-2", allowed_categories=frozenset({"ai_fast"}))

    assert first_lease is not None and first_lease.id == first_ids[0]
    assert second_lease is not None and second_lease.id == second_id


@pytest.mark.asyncio
async def test_category_limit_is_shared_between_queue_instances(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    first_queue = SQLiteJobQueue(session_factory, category_limits={"ai_fast": 1})
    second_queue = SQLiteJobQueue(session_factory, category_limits={"ai_fast": 1})
    await first_queue.enqueue(
        "signal.ai_triage", {"index": 1}, tenant_id=tenant.id, category="ai_fast"
    )
    await first_queue.enqueue(
        "signal.ai_triage", {"index": 2}, tenant_id=tenant.id, category="ai_fast"
    )

    first = await first_queue.claim_next("process-a", allowed_categories=frozenset({"ai_fast"}))
    blocked = await second_queue.claim_next(
        "process-b", allowed_categories=frozenset({"ai_fast"})
    )

    assert first is not None
    assert blocked is None


@pytest.mark.asyncio
async def test_total_ai_limit_spans_fast_heavy_report_and_reconciliation(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    resources = {"ai": (frozenset({"ai_fast", "ai_heavy"}), 1)}
    job_types = {"ai": frozenset({"signal.ai_triage", "report_generation"})}
    first_queue = SQLiteJobQueue(
        session_factory, resource_limits=resources, resource_job_types=job_types
    )
    second_queue = SQLiteJobQueue(
        session_factory, resource_limits=resources, resource_job_types=job_types
    )
    fast_id = await first_queue.enqueue(
        "signal.ai_triage",
        {},
        tenant_id=tenant.id,
        category="ai_fast",
        cost_class="ai_fast",
    )
    await first_queue.enqueue(
        "report_generation",
        {},
        tenant_id=tenant.id,
        category="report",
        cost_class="heavy",
    )

    fast = await first_queue.claim_next("process-a")
    blocked = await second_queue.claim_next("process-b")

    assert fast is not None and fast.id == fast_id
    assert blocked is None


@pytest.mark.asyncio
async def test_ten_tenant_ai_burst_drains_without_duplicates_or_starvation(
    session_factory, make_service, tenant_payload
) -> None:
    tenants = []
    async with session_factory() as session:
        for index in range(10):
            tenants.append(
                await make_service(session).create_tenant(
                    tenant_payload.model_copy(update={
                        "name": f"Load tenant {index}",
                        "owner_telegram_username": f"load_owner_{index}",
                        "owner_telegram_user_id": 555001000 + index,
                    })
                )
            )
    queue = SQLiteJobQueue(
        session_factory,
        category_limits={"ai_fast": 4},
        resource_limits={"ai": (frozenset({"ai_fast", "ai_heavy"}), 2)},
    )
    job_ids = [
        await queue.enqueue(
            "test.ai",
            {"tenant": tenant.id, "index": index},
            tenant_id=tenant.id,
            category="ai_fast",
            cost_class="ai_fast",
        )
        for tenant in tenants
        for index in range(3)
    ]
    active = 0
    peak_active = 0
    executions: list[str] = []
    lock = asyncio.Lock()

    async def handler(lease):
        nonlocal active, peak_active
        async with lock:
            active += 1
            peak_active = max(peak_active, active)
            executions.append(lease.id)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return {"tenant_id": lease.tenant_id}

    workers = [
        BackgroundWorker(
            queue,
            f"ai-load-{index}",
            {"test.ai": handler},
            heartbeat_seconds=0.01,
            allowed_categories=frozenset({"ai_fast"}),
        )
        for index in range(4)
    ]

    async def drain(worker):
        while await worker.run_once():
            pass

    await asyncio.gather(*(drain(worker) for worker in workers))
    jobs = [await queue.get(job_id) for job_id in job_ids]

    assert peak_active == 2
    assert len(executions) == len(set(executions)) == 30
    assert all(job.status == "completed" for job in jobs)
    assert {job.tenant_id for job in jobs} == {tenant.id for tenant in tenants}


@pytest.mark.asyncio
async def test_telegram_rpc_bypasses_tenant_general_concurrency(
    session_factory, make_service, tenant_payload
) -> None:
    queue = SQLiteJobQueue(session_factory, max_active_tenant_jobs=1)
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, telegram_user_id=100, status="ready")
        session.add(connection)
        await session.commit()
        tenant_id = tenant.id
        account_id = connection.id
    await queue.enqueue("analysis.pipeline", {}, tenant_id=tenant_id, category="analysis")
    rpc_id = await queue.enqueue(
        "telegram.catch_up",
        {},
        tenant_id=tenant_id,
        telegram_account_id=account_id,
        category="telegram_rpc",
    )
    general = await queue.claim_next("general", allowed_categories=frozenset({"analysis"}))
    assert general is not None
    rpc = await queue.claim_next(
        "actor",
        allowed_categories=frozenset({"telegram_rpc"}),
        telegram_account_id=account_id,
    )
    assert rpc is not None and rpc.id == rpc_id


@pytest.mark.asyncio
async def test_queue_detects_unfinished_job_for_telegram_account(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, status="ready")
        session.add(connection)
        await session.commit()
        tenant_id = tenant.id
        account_id = connection.id

    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue(
        "telegram.catch_up",
        {},
        tenant_id=tenant_id,
        telegram_account_id=account_id,
        category="telegram_rpc",
    )
    assert await queue.has_unfinished("telegram.catch_up", telegram_account_id=account_id)
    lease = await queue.claim_next("telegram-test", telegram_account_id=account_id)
    assert lease is not None and lease.id == job_id
    await queue.complete(lease)
    assert not await queue.has_unfinished("telegram.catch_up", telegram_account_id=account_id)


@pytest.mark.asyncio
async def test_telegram_runtime_lease_fences_previous_owner(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        connection = TelegramConnection(tenant_id=tenant.id, telegram_user_id=100, status="ready")
        session.add(connection)
        await session.commit()

    leases = TelegramRuntimeLeaseStore(session_factory, ttl_seconds=30)
    first = await leases.acquire(connection.id, "runtime-a")
    assert first is not None and first.generation == 1
    assert await leases.acquire(connection.id, "runtime-b") is None

    async with session_factory() as session:
        await session.execute(
            update(TelegramRuntimeLease)
            .where(TelegramRuntimeLease.connection_id == connection.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    second = await leases.acquire(connection.id, "runtime-b")
    assert second is not None and second.generation == 2
    assert not await leases.is_current(first)
    assert await leases.heartbeat(first) is None
    assert not await leases.release(first)
    assert await leases.is_current(second)


@pytest.mark.asyncio
async def test_partitioned_jobs_are_claimed_in_sequence(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    later = await queue.enqueue(
        "system.echo", {"order": 2}, partition_key="dialog:1", partition_sequence=2
    )
    earlier = await queue.enqueue(
        "system.echo", {"order": 1}, partition_key="dialog:1", partition_sequence=1
    )
    first = await queue.claim_next("ordered-worker")
    assert first is not None and first.id == earlier
    assert await queue.claim_next("parallel-worker") is None
    await queue.complete(first)
    second = await queue.claim_next("ordered-worker")
    assert second is not None and second.id == later


@pytest.mark.asyncio
async def test_partition_predecessor_is_not_hidden_beyond_candidate_limit(session_factory) -> None:
    queue = SQLiteJobQueue(session_factory)
    now = datetime.now(UTC)
    for sequence in range(2, 103):
        await queue.enqueue(
            "system.echo",
            {"order": sequence},
            scheduled_at=now - timedelta(minutes=2),
            partition_key="busy-dialog",
            partition_sequence=sequence,
        )
    predecessor = await queue.enqueue(
        "system.echo",
        {"order": 1},
        scheduled_at=now - timedelta(minutes=1),
        partition_key="busy-dialog",
        partition_sequence=1,
    )

    claimed = await queue.claim_next("ordered-worker")
    assert claimed is not None and claimed.id == predecessor


def test_consistent_backup_and_restore(tmp_path) -> None:
    source = tmp_path / "app.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO example(value) VALUES ('before-backup')")
    backup = backup_database(source, tmp_path / "backups")
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO example(value) VALUES ('after-backup')")
    restored = restore_database(backup, tmp_path / "restored.db")
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("before-backup",)
        ]
