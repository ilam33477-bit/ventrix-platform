from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

T = TypeVar("T")
_PROCESS_WRITE_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)


def process_write_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _PROCESS_WRITE_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
        _PROCESS_WRITE_SEMAPHORES[loop] = semaphore
    return semaphore


class Base(DeclarativeBase):
    pass


def ensure_sqlite_directory(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database:
        raise ValueError("MVP requires DATABASE_URL=sqlite+aiosqlite:///./data/app.db")
    database_path = Path(url.database).expanduser()
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return database_path.resolve()


def build_engine(
    database_url: str | None = None, busy_timeout_ms: int | None = None
) -> AsyncEngine:
    settings = get_settings() if database_url is None or busy_timeout_ms is None else None
    url = database_url or settings.database_url
    ensure_sqlite_directory(url)
    timeout = busy_timeout_ms or settings.sqlite_busy_timeout_ms
    engine = create_async_engine(url, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={int(timeout)}")
        finally:
            cursor.close()

    return engine


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def is_database_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


class SQLiteTransactionManager:
    """Serializes writes per process and retries only transient SQLite lock errors."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_attempts: int = 5,
        base_delay_seconds: float = 0.05,
    ) -> None:
        self.session_factory = session_factory
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds

    async def run(self, operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async with process_write_semaphore():
            for attempt in range(self.max_attempts):
                async with self.session_factory() as session:
                    try:
                        async with session.begin():
                            return await operation(session)
                    except OperationalError as exc:
                        await session.rollback()
                        if not is_database_locked(exc) or attempt + 1 >= self.max_attempts:
                            raise
                        await asyncio.sleep(self.base_delay_seconds * (2**attempt))
        raise RuntimeError("unreachable SQLite retry state")
