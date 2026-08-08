from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import tempfile
from datetime import time
from pathlib import Path

import httpx
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from services.backend import database
from services.backend.bot.sqlite_storage import SQLiteFSMStorage
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.jobs.worker import HANDLERS, BackgroundWorker
from services.backend.models import AuditLog, BotInstance, EncryptedSecret, Tenant
from services.backend.schemas import AIProfileUpdate, BotCreate, TenantCreate
from services.backend.scripts.backup_sqlite import backup_database, restore_database
from services.backend.services.encryption import EncryptionService
from services.backend.services.foundation import FoundationService
from services.backend.services.telegram import VerifiedBot


class MockTelegramVerifier:
    async def verify(self, token: str) -> VerifiedBot:
        if len(token) < 20:
            raise ValueError("mock token rejected")
        return VerifiedBot(bot_id=700000001, username="smoke_client_bot", display_name="Smoke Client")


def configure_environment(database_path: Path) -> tuple[str, str]:
    owner_token = secrets.token_urlsafe(32)
    encryption_key = Fernet.generate_key().decode()
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
            "PLATFORM_OWNER_TELEGRAM_ID": "100200300",
            "PLATFORM_OWNER_TELEGRAM_USERNAME": "smoke_owner",
            "TELEGRAM_OWNER_BOT_TOKEN": "mock-" + secrets.token_urlsafe(24),
            "OWNER_API_TOKEN": owner_token,
            "APP_ENCRYPTION_KEY": encryption_key,
            "TELEGRAM_API_BASE_URL": "https://telegram.invalid",
        }
    )
    return owner_token, encryption_key


async def exercise(database_path: Path, backup_directory: Path, owner_token: str, key: str) -> None:
    from services.backend.config import get_settings

    get_settings.cache_clear()
    engine = database.build_engine(f"sqlite+aiosqlite:///{database_path}", 5000)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    database._engine = engine
    database._session_factory = session_factory

    payload = TenantCreate(
        name="Smoke Company",
        owner_name="Smoke Owner",
        owner_telegram_username="smoke_owner",
        owner_telegram_user_id=555000111,
        niche="B2B services",
        business_description="Reproducible local smoke company",
        products_services="Implementation, support",
        target_audience="Operations directors",
        working_hours={"description": "Mon-Fri 09:00-18:00"},
        timezone="Europe/Moscow",
        response_sla_minutes=60,
        critical_problem_criteria="Customer waits too long",
        daily_report_time=time(9, 30),
    )
    async with session_factory() as session:
        service = FoundationService(
            session,
            100200300,
            EncryptionService(key),
            MockTelegramVerifier(),
            "smoke",
            "smoke_owner",
        )
        tenant = await service.create_tenant(payload)
        await service.update_ai_profile(
            tenant.id, AIProfileUpdate(typical_processes=["qualification"], sales_stages=["new"])
        )
        bot = await service.create_bot(
            tenant.id, BotCreate(token="mock-" + secrets.token_urlsafe(24))
        )
        tenant_id, bot_id = tenant.id, bot.id

    from services.backend.api.app import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://smoke"
    ) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
        tenant_response = await client.get(
            f"/api/v1/owner/tenants/{tenant_id}", headers={"X-Owner-Token": owner_token}
        )
    assert health.status_code == 200 and ready.status_code == 200
    assert tenant_response.status_code == 200 and tenant_response.json()["name"] == "Smoke Company"

    queue = SQLiteJobQueue(session_factory)
    job_id = await queue.enqueue("system.echo", {"smoke": True}, idempotency_key="smoke-job")
    assert await BackgroundWorker(queue, "smoke:1", HANDLERS, heartbeat_seconds=1).run_once()
    assert (await queue.get(job_id)).status == "completed"

    storage = SQLiteFSMStorage(session_factory)
    from aiogram.fsm.storage.base import StorageKey

    storage_key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    await storage.set_state(storage_key, "Smoke:state")
    await engine.dispose()

    restarted_engine = database.build_engine(f"sqlite+aiosqlite:///{database_path}", 5000)
    restarted_factory = async_sessionmaker(
        restarted_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with restarted_factory() as session:
        assert await session.scalar(select(func.count(Tenant.id))) == 1
        assert await session.scalar(select(func.count(BotInstance.id))) == 1
        assert await session.scalar(select(func.count(EncryptedSecret.id))) == 1
        assert await session.scalar(select(func.count(AuditLog.id))) >= 3
    assert await SQLiteFSMStorage(restarted_factory).get_state(storage_key) == "Smoke:state"
    await restarted_engine.dispose()

    backup = backup_database(database_path, backup_directory)
    restored = restore_database(backup, backup_directory / "restored.db")
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM tenants").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM bot_instances").fetchone() == (1,)
    assert bot_id


def run_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="opera-smoke-") as temp:
        root = Path(temp)
        database_path = root / "smoke.db"
        owner_token, key = configure_environment(database_path)
        command.upgrade(Config("alembic.ini"), "head")
        asyncio.run(exercise(database_path, root / "backups", owner_token, key))


def main() -> None:
    try:
        run_smoke()
    except Exception as exc:
        print(f"FAIL: smoke test ({type(exc).__name__})")
        raise SystemExit(1) from exc
    print("PASS: migrations, API, tenant, AI profile, encrypted bot, job, FSM, restart, backup/restore")


if __name__ == "__main__":
    main()
