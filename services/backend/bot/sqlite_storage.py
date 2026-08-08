from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..models import FSMState


def serialize_fsm_value(value: Any) -> Any:
    """Convert common domain values to JSON-safe FSM data without type metadata."""
    if isinstance(value, Enum):
        return serialize_fsm_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): serialize_fsm_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize_fsm_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported FSM data type: {type(value).__name__}")


class SQLiteFSMStorage(BaseStorage):
    """Durable aiogram FSM storage shared through the application SQLite file."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl: timedelta = timedelta(hours=72),
    ) -> None:
        self.session_factory = session_factory
        self.ttl = ttl
        self.transactions = SQLiteTransactionManager(session_factory)

    @staticmethod
    def _filters(key: StorageKey):
        return and_(
            FSMState.bot_id == key.bot_id,
            FSMState.chat_id == key.chat_id,
            FSMState.user_id == key.user_id,
            FSMState.thread_id == (key.thread_id or 0),
            FSMState.business_connection_id == (key.business_connection_id or ""),
            FSMState.destiny == (key.destiny or "default"),
        )

    def _new_row(self, key: StorageKey) -> FSMState:
        now = datetime.now(UTC)
        return FSMState(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id or 0,
            business_connection_id=key.business_connection_id or "",
            destiny=key.destiny or "default",
            state=None,
            data_json={},
            expires_at=now + self.ttl,
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_value = state.state if isinstance(state, State) else state

        async def write(session: AsyncSession) -> None:
            row = await session.scalar(select(FSMState).where(self._filters(key)))
            if row is None:
                if state_value is None:
                    return
                row = self._new_row(key)
                session.add(row)
            row.state = state_value
            row.expires_at = datetime.now(UTC) + self.ttl
            if row.state is None and not row.data_json:
                await session.delete(row)

        await self.transactions.run(write)

    async def get_state(self, key: StorageKey) -> str | None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            return await session.scalar(
                select(FSMState.state).where(
                    self._filters(key),
                    or_(FSMState.expires_at.is_(None), FSMState.expires_at > now),
                )
            )

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        safe_data = serialize_fsm_value(data)

        async def write(session: AsyncSession) -> None:
            row = await session.scalar(select(FSMState).where(self._filters(key)))
            if row is None:
                if not safe_data:
                    return
                row = self._new_row(key)
                session.add(row)
            row.data_json = safe_data
            row.expires_at = datetime.now(UTC) + self.ttl
            if row.state is None and not row.data_json:
                await session.delete(row)

        await self.transactions.run(write)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            value = await session.scalar(
                select(FSMState.data_json).where(
                    self._filters(key),
                    or_(FSMState.expires_at.is_(None), FSMState.expires_at > now),
                )
            )
            return dict(value or {})

    async def cleanup_expired(self) -> int:
        async def write(session: AsyncSession) -> int:
            result = await session.execute(
                delete(FSMState).where(
                    FSMState.expires_at.is_not(None), FSMState.expires_at <= datetime.now(UTC)
                )
            )
            return int(result.rowcount or 0)

        return await self.transactions.run(write)

    async def close(self) -> None:
        return None
