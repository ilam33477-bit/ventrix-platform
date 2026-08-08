from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from uuid import UUID

import pytest
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select, update

from services.backend.bot.sqlite_storage import SQLiteFSMStorage
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.jobs.worker import HANDLERS, BackgroundWorker
from services.backend.models import BackgroundJob, FSMState
from services.backend.scripts.backup_sqlite import backup_database, restore_database


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
