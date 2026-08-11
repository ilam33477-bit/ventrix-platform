from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from ..models import (
    Employee,
    EmployeeTelegramAccount,
    MonitoredSource,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramIncrementalCursor,
    TelegramMessage,
)


class TelegramEventIngestion:
    """Durably applies normalized live/catch-up events; no AI or network RPC lives here."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.transactions = SQLiteTransactionManager(session_factory)
        self._dialog_locks: dict[str, asyncio.Lock] = {}

    async def ingest(self, job: JobLease) -> dict[str, Any]:
        if job.tenant_id is None or job.telegram_account_id is None:
            raise ValueError("tenant and Telegram connection are required")
        peer_id = str(job.payload["canonical_peer_id"])
        partition = f"{job.tenant_id}:{peer_id}"
        lock = self._dialog_locks.setdefault(partition, asyncio.Lock())
        async with lock:
            result = await self.transactions.run(lambda session: self._apply(session, job, peer_id))
        if result.get("message_id"):
            await self.queue.enqueue(
                "signal.scan_batch",
                {
                    "message_ids": [result["message_id"]],
                    "rescan": bool(result["edited"]),
                },
                tenant_id=job.tenant_id,
                telegram_account_id=job.telegram_account_id,
                dialog_id=result["dialog_id"],
                priority=JOB_PRIORITY["P1"],
                idempotency_key=f"signal-scan-event:{job.id}",
                correlation_id=job.correlation_id or job.id,
                category="realtime",
                cost_class="light",
                partition_key=f"business-dialog:{job.tenant_id}:{result['dialog_id']}",
                partition_sequence=int(result["telegram_message_id"]),
            )
        return result

    async def _apply(self, session: AsyncSession, job: JobLease, peer_id: str) -> dict[str, Any]:
        connection = await session.scalar(
            select(TelegramConnection).where(
                TelegramConnection.id == job.telegram_account_id,
                TelegramConnection.tenant_id == job.tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
        )
        if connection is None:
            raise LookupError("Telegram connection not found")
        source_type = str(job.payload.get("source_type") or "personal")
        if source_type == "personal" and bool(job.payload.get("is_automated")):
            dialog = await session.scalar(
                select(TelegramDialog).where(
                    TelegramDialog.connection_id == connection.id,
                    TelegramDialog.canonical_peer_id == peer_id,
                )
            )
            if dialog is not None:
                dialog.classification = "automated_account"
                dialog.selected = False
                dialog.excluded = True
            return {"ignored": True, "reason": "automated_account"}
        monitored = source_type == "personal"
        if source_type != "personal":
            monitored = bool(
                await session.scalar(
                    select(MonitoredSource.id).where(
                        MonitoredSource.tenant_id == job.tenant_id,
                        MonitoredSource.connection_id == connection.id,
                        MonitoredSource.canonical_peer_id == peer_id,
                        MonitoredSource.enabled.is_(True),
                    )
                )
            )
        if not monitored:
            return {"ignored": True, "reason": "source_not_monitored"}

        remote_message_id = int(job.payload["telegram_message_id"])
        dialog = await session.scalar(
            select(TelegramDialog).where(
                TelegramDialog.connection_id == connection.id,
                TelegramDialog.canonical_peer_id == peer_id,
            )
        )
        if dialog is None:
            dialog = TelegramDialog(
                tenant_id=job.tenant_id,
                connection_id=connection.id,
                telegram_dialog_id=int(job.payload.get("telegram_dialog_id") or peer_id),
                canonical_peer_id=peer_id,
                title=str(job.payload.get("dialog_title") or "Telegram dialog")[:300],
                username=job.payload.get("dialog_username"),
                dialog_type=source_type,
                source="live",
                selected=True,
                excluded=False,
                classification="auto_personal" if source_type == "personal" else "opt_in",
            )
            session.add(dialog)
            await session.flush()
        elif source_type == "personal":
            dialog.selected = True
            dialog.excluded = False

        existing = await session.scalar(
            select(TelegramMessage).where(
                TelegramMessage.dialog_id == dialog.id,
                TelegramMessage.telegram_message_id == remote_message_id,
            )
        )
        edited_at = self._datetime(job.payload.get("edited_at"))
        if existing is not None:
            if job.payload.get("event_type") != "edited" or (
                existing.edited_at is not None
                and edited_at is not None
                and self._aware(existing.edited_at) >= edited_at
            ):
                connection.duplicate_events += 1
                return {"duplicate": True, "dialog_id": dialog.id}
            existing.body_text = job.payload.get("text")
            existing.attachments_json = list(job.payload.get("attachments") or [])
            existing.edited_at = edited_at or datetime.now(UTC)
            existing.sender_role = await self._sender_role(session, connection, job.payload)
            message = existing
            await session.execute(
                Signal.__table__.update()
                .where(Signal.source_message_id == existing.id)
                .values(status="superseded")
            )
            was_edited = True
        else:
            message = TelegramMessage(
                tenant_id=job.tenant_id,
                connection_id=connection.id,
                dialog_id=dialog.id,
                telegram_message_id=remote_message_id,
                sender_id=job.payload.get("sender_id"),
                sender_username=job.payload.get("sender_username"),
                sender_role=await self._sender_role(session, connection, job.payload),
                sent_at=self._datetime(job.payload.get("sent_at")) or datetime.now(UTC),
                edited_at=edited_at,
                outgoing=bool(job.payload.get("outgoing")),
                body_text=job.payload.get("text"),
                attachments_json=list(job.payload.get("attachments") or []),
                ingestion_source=str(job.payload.get("ingestion_source") or "live"),
            )
            session.add(message)
            await session.flush()
            was_edited = False

        cursor = await session.scalar(
            select(TelegramIncrementalCursor).where(
                TelegramIncrementalCursor.connection_id == connection.id,
                TelegramIncrementalCursor.dialog_id == dialog.id,
            )
        )
        if cursor is None:
            cursor = TelegramIncrementalCursor(
                tenant_id=job.tenant_id,
                connection_id=connection.id,
                dialog_id=dialog.id,
            )
            session.add(cursor)
        cursor.last_message_id = max(cursor.last_message_id or 0, remote_message_id)
        cursor.last_event_at = message.sent_at
        cursor.last_sync_at = datetime.now(UTC)
        dialog.last_message_id = max(dialog.last_message_id, remote_message_id)
        previous_at = self._aware(dialog.last_message_at) if dialog.last_message_at else None
        message_at = self._aware(message.sent_at)
        dialog.last_message_at = max(previous_at, message_at) if previous_at else message_at
        connection.last_event_at = message.sent_at
        return {
            "message_id": message.id,
            "dialog_id": dialog.id,
            "edited": was_edited,
            "duplicate": False,
            "telegram_message_id": remote_message_id,
        }

    @staticmethod
    async def _sender_role(
        session: AsyncSession,
        connection: TelegramConnection,
        payload: dict[str, Any],
    ) -> str:
        if bool(payload.get("outgoing")) or payload.get("sender_id") == connection.telegram_user_id:
            return "account_owner"
        sender_id = payload.get("sender_id")
        if sender_id is None:
            return "unknown"
        employee = await session.scalar(
            select(Employee.id).where(
                Employee.tenant_id == connection.tenant_id,
                Employee.telegram_user_id == sender_id,
            )
        ) or await session.scalar(
            select(EmployeeTelegramAccount.employee_id).where(
                EmployeeTelegramAccount.tenant_id == connection.tenant_id,
                EmployeeTelegramAccount.telegram_user_id == sender_id,
            )
        )
        if employee:
            return "employee"
        return "external" if payload.get("source_type") == "group" else "customer"

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return TelegramEventIngestion._aware(value) if value else None
        parsed = datetime.fromisoformat(str(value))
        return TelegramEventIngestion._aware(parsed)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
