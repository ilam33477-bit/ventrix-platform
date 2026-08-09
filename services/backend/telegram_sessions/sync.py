from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import JOB_PRIORITY, JobLease, SQLiteJobQueue
from ..models import (
    Employee,
    EmployeeTelegramAccount,
    EncryptedSecret,
    InitialAnalysisRun,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TelegramSyncCursor,
    TenantAIProfile,
)
from ..services.encryption import EncryptionService
from .gateway import MessageBatch, TelegramFloodWait, TelegramUserGateway


class TelegramChatSyncRetry(RuntimeError):
    pass


class TelegramSyncHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        gateway: TelegramUserGateway,
        *,
        batch_size: int = 100,
        batch_pause_seconds: float = 1.0,
        max_messages_per_chat: int = 2000,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.gateway = gateway
        self.batch_size = batch_size
        self.batch_pause_seconds = batch_pause_seconds
        self.max_messages_per_chat = max_messages_per_chat
        self.transactions = SQLiteTransactionManager(session_factory)
        self.queue = SQLiteJobQueue(session_factory)

    async def sync_chat(self, job: JobLease) -> dict[str, object]:
        """Legacy adapter kept for tests; production dispatches this RPC inside the account actor."""
        cursor_id = str(job.payload["cursor_id"])
        loaded = await self._load(cursor_id, job.tenant_id)
        if loaded is None:
            return {"skipped": "cursor_missing"}
        cursor, run, connection, dialog, session_string = loaded
        if run.stop_requested or connection.stop_requested:
            await self._mark_cursor(cursor_id, "stopped", "stop_requested")
            await self._refresh_run(run.id)
            return {"stopped": True}
        try:
            batch = await self.gateway.fetch_messages(
                session_string,
                dialog.telegram_dialog_id,
                offset_id=cursor.offset_message_id,
                limit=self.batch_size,
            )
        except TelegramFloodWait:
            raise
        except Exception as exc:  # noqa: BLE001 - one chat failure must not stop the run
            if job.attempts < 2:
                raise TelegramChatSyncRetry("temporary Telegram chat sync failure") from None
            await self._mark_cursor(cursor_id, "failed", type(exc).__name__)
            await self._refresh_run(run.id)
            return {"failed": type(exc).__name__}
        finally:
            session_string = ""

        return await self.process_actor_batch(job, cursor, run, connection, dialog, batch)

    async def load_actor_context(self, cursor_id: str, tenant_id: str | None):
        """Load DB-only sync context without decrypting or exposing a StringSession."""
        async with self.session_factory() as session:
            cursor = await session.scalar(
                select(TelegramSyncCursor).where(
                    TelegramSyncCursor.id == cursor_id,
                    TelegramSyncCursor.tenant_id == tenant_id,
                )
            )
            if cursor is None:
                return None
            run = await session.get(InitialAnalysisRun, cursor.run_id)
            connection = await session.get(TelegramConnection, cursor.connection_id)
            dialog = await session.get(TelegramDialog, cursor.dialog_id)
            return cursor, run, connection, dialog

    async def process_actor_batch(
        self,
        job: JobLease,
        cursor: TelegramSyncCursor,
        run: InitialAnalysisRun,
        connection: TelegramConnection,
        dialog: TelegramDialog,
        batch: MessageBatch,
    ) -> dict[str, object]:
        """Persist and advance a batch fetched by the sole Telethon session actor."""
        cursor_id = cursor.id

        cutoff = datetime.now(UTC) - timedelta(days=run.history_days)
        accepted = [item for item in batch.messages if item.sent_at >= cutoff]
        reached_cutoff = any(item.sent_at < cutoff for item in batch.messages)
        await self._store_batch(cursor_id, accepted, batch)
        async with self.session_factory() as session:
            current = await session.get(TelegramSyncCursor, cursor_id)
            fetched = current.fetched_messages
            run_id = current.run_id
        has_more = (
            batch.has_more
            and not reached_cutoff
            and fetched < self.max_messages_per_chat
            and batch.next_offset_id != cursor.offset_message_id
        )
        if has_more:
            await asyncio.sleep(self.batch_pause_seconds)
            await self.queue.enqueue(
                "telegram.sync_chat",
                {"cursor_id": cursor_id},
                tenant_id=job.tenant_id,
                telegram_account_id=connection.id,
                idempotency_key=f"telegram-sync:{cursor_id}:{batch.next_offset_id}",
                category="telegram_rpc",
                cost_class="light",
                max_attempts=8,
            )
        else:
            await self._mark_cursor(cursor_id, "completed", None)
        await self._refresh_run(run_id)
        return {"messages": len(accepted), "has_more": has_more}

    async def _load(self, cursor_id: str, tenant_id: str | None):
        async with self.session_factory() as session:
            cursor = await session.scalar(
                select(TelegramSyncCursor).where(
                    TelegramSyncCursor.id == cursor_id,
                    TelegramSyncCursor.tenant_id == tenant_id,
                )
            )
            if cursor is None:
                return None
            run = await session.get(InitialAnalysisRun, cursor.run_id)
            connection = await session.get(TelegramConnection, cursor.connection_id)
            dialog = await session.get(TelegramDialog, cursor.dialog_id)
            secret = await session.get(EncryptedSecret, connection.session_secret_id)
            return cursor, run, connection, dialog, self.encryption.decrypt(secret.ciphertext)

    async def _store_batch(self, cursor_id: str, messages: list, batch: MessageBatch) -> None:
        async def write(session: AsyncSession) -> None:
            cursor = await session.get(TelegramSyncCursor, cursor_id)
            dialog = await session.get(TelegramDialog, cursor.dialog_id)
            existing_ids = (
                set(
                    await session.scalars(
                        select(TelegramMessage.telegram_message_id).where(
                            TelegramMessage.dialog_id == cursor.dialog_id,
                            TelegramMessage.telegram_message_id.in_([item.id for item in messages]),
                        )
                    )
                )
                if messages
                else set()
            )
            sender_ids = {item.sender_id for item in messages if item.sender_id is not None}
            employee_sender_ids = set(
                await session.scalars(
                    select(Employee.telegram_user_id).where(
                        Employee.tenant_id == cursor.tenant_id,
                        Employee.telegram_user_id.in_(sender_ids),
                    )
                )
            ) | set(
                await session.scalars(
                    select(EmployeeTelegramAccount.telegram_user_id).where(
                        EmployeeTelegramAccount.tenant_id == cursor.tenant_id,
                        EmployeeTelegramAccount.telegram_user_id.in_(sender_ids),
                    )
                )
            )
            for item in messages:
                if item.id in existing_ids:
                    continue
                session.add(
                    TelegramMessage(
                        tenant_id=cursor.tenant_id,
                        connection_id=cursor.connection_id,
                        dialog_id=cursor.dialog_id,
                        telegram_message_id=item.id,
                        sender_id=item.sender_id,
                        sender_username=item.sender_username,
                        sender_role=(
                            "account_owner"
                            if item.outgoing
                            else "employee"
                            if item.sender_id in employee_sender_ids
                            else "external"
                            if dialog.dialog_type == "group"
                            else "customer"
                        ),
                        ingestion_source="history",
                        sent_at=item.sent_at,
                        edited_at=item.edited_at,
                        outgoing=item.outgoing,
                        body_text=item.text,
                        attachments_json=item.attachments,
                    )
                )
            cursor.offset_message_id = batch.next_offset_id
            cursor.fetched_messages += len(
                [item for item in messages if item.id not in existing_ids]
            )
            cursor.status = "running"
            cursor.last_batch_at = datetime.now(UTC)
            cursor.last_error_code = None
            if messages:
                newest = max(messages, key=lambda item: item.id)
                dialog.last_message_id = max(dialog.last_message_id, newest.id)
                dialog.last_message_at = newest.sent_at
            dialog.last_sync_at = datetime.now(UTC)

        await self.transactions.run(write)

    async def _mark_cursor(self, cursor_id: str, status: str, error_code: str | None) -> None:
        async def write(session: AsyncSession) -> None:
            cursor = await session.get(TelegramSyncCursor, cursor_id)
            if cursor:
                cursor.status = status
                cursor.last_error_code = error_code

        await self.transactions.run(write)

    async def _refresh_run(self, run_id: str) -> None:
        should_analyze = False

        async def write(session: AsyncSession) -> None:
            nonlocal should_analyze
            run = await session.get(InitialAnalysisRun, run_id)
            cursors = list(
                await session.scalars(
                    select(TelegramSyncCursor).where(TelegramSyncCursor.run_id == run_id)
                )
            )
            completed = sum(item.status == "completed" for item in cursors)
            failed = sum(item.status == "failed" for item in cursors)
            stopped = sum(item.status == "stopped" for item in cursors)
            run.completed_dialogs = completed
            run.failed_dialogs = failed
            run.messages_loaded = sum(item.fetched_messages for item in cursors)
            terminal = completed + failed + stopped
            run.progress_percent = min(85, 30 + int(55 * terminal / max(1, len(cursors))))
            run.stage = "history_sync"
            connection = await session.get(TelegramConnection, run.connection_id)
            connection.progress_percent = run.progress_percent
            connection.progress_stage = run.stage
            connection.progress_json = {
                "dialogs_total": len(cursors),
                "dialogs_completed": terminal,
                "messages_loaded": run.messages_loaded,
            }
            should_analyze = terminal == len(cursors) and not run.stop_requested
            if stopped and terminal == len(cursors):
                run.status = "stopped"
                run.stage = "stopped"
                run.finished_at = datetime.now(UTC)

        await self.transactions.run(write)
        if should_analyze:
            await self._analyze(run_id)

    async def _analyze(self, run_id: str) -> None:
        async with self.session_factory() as session:
            run = await session.get(InitialAnalysisRun, run_id)
            profile = await session.scalar(
                select(TenantAIProfile).where(TenantAIProfile.tenant_id == run.tenant_id)
            )
            profile_terms = {
                word.lower()
                for value in (
                    [profile.niche, profile.business_description]
                    + profile.products
                    + profile.typical_processes
                )
                for word in value.split()
                if len(word) >= 4
            }
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.connection_id == run.connection_id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            )
            payload: list[tuple[TelegramDialog, list[TelegramMessage]]] = []
            for dialog in dialogs:
                messages = list(
                    await session.scalars(
                        select(TelegramMessage)
                        .where(TelegramMessage.dialog_id == dialog.id)
                        .order_by(TelegramMessage.sent_at.desc())
                        .limit(30)
                    )
                )
                payload.append((dialog, messages))

        async def write(session: AsyncSession) -> None:
            run = await session.get(InitialAnalysisRun, run_id)
            connection = await session.get(TelegramConnection, run.connection_id)
            metrics = {
                "connected_account": connection.display_name
                or connection.phone_masked
                or "Telegram",
                "working_folder": connection.selected_folder_title or "",
                "promises": 0,
                "potential_deals": 0,
                "clients_without_answer": 0,
                "overdue_commitments": 0,
                "complaints": 0,
                "calls_at_risk": 0,
                "system_gaps": run.failed_dialogs,
                "problems_created": 0,
                "analysis_jobs_queued": 0,
                "analyzed_dialogs": len(payload),
                "working_groups": sum(dialog.dialog_type == "group" for dialog, _ in payload),
                "working_channels": sum(dialog.dialog_type == "channel" for dialog, _ in payload),
                "personal_dialogs": sum(dialog.dialog_type == "personal" for dialog, _ in payload),
                "probable_business_personal_dialogs": 0,
            }
            for dialog, messages in payload:
                texts = " ".join((item.body_text or "").lower() for item in messages[:10])
                if dialog.dialog_type == "personal":
                    client_terms = (
                        "договор",
                        "оплат",
                        "цена",
                        "клиент",
                        "заказ",
                        "коммерческ",
                        "предложен",
                    )
                    employee_terms = ("задач", "дедлайн", "коллег", "план", "отчёт", "команда")
                    contractor_terms = ("подряд", "исполнител", "акт", "счёт", "услуг")
                    personal_terms = ("семья", "ужин", "домой", "отдых", "фото", "день рождения")
                    scores = {
                        "probable_client": sum(term in texts for term in client_terms),
                        "probable_employee": sum(term in texts for term in employee_terms),
                        "contractor": sum(term in texts for term in contractor_terms),
                    }
                    profile_hits = sum(term in texts for term in profile_terms)
                    classification, hits = max(scores.items(), key=lambda item: item[1])
                    hits += min(2, profile_hits)
                    personal_hits = sum(term in texts for term in personal_terms)
                    dialog_db = await session.get(TelegramDialog, dialog.id)
                    if personal_hits >= 2 and hits == 0:
                        dialog_db.classification = "personal_contact"
                        dialog_db.confidence = min(0.98, 0.7 + personal_hits * 0.08)
                        dialog_db.requires_user_confirmation = False
                        # Source policy monitors every personal dialog. Relevance is
                        # decided per message downstream, not by silently removing a chat.
                        dialog_db.selected = True
                        dialog_db.excluded = False
                        continue
                    frequency_boost = 0.08 if len(messages) >= 10 else 0
                    dialog_db.confidence = min(0.96, 0.32 + hits * 0.14 + frequency_boost)
                    dialog_db.classification = classification if hits >= 2 else "unknown"
                    dialog_db.requires_user_confirmation = False
                    dialog_db.selected = True
                    dialog_db.excluded = False
                    metrics["probable_business_personal_dialogs"] += 1
                if not messages:
                    continue
                latest = messages[0]
                latest_sent_at = (
                    latest.sent_at
                    if latest.sent_at.tzinfo is not None
                    else latest.sent_at.replace(tzinfo=UTC)
                )
                candidates: list[tuple[str, str, float]] = []
                if not latest.outgoing and datetime.now(UTC) - latest_sent_at > timedelta(hours=1):
                    candidates.append(("client_without_answer", "high", 0.9))
                    metrics["clients_without_answer"] += 1
                if any(term in texts for term in ("жалоб", "возврат", "недоволен")):
                    candidates.append(("complaint", "high", 0.86))
                    metrics["complaints"] += 1
                if any(term in texts for term in ("обещаю", "пришлю", "сделаю", "до завтра")):
                    metrics["promises"] += 1
                if any(term in texts for term in ("цена", "стоимость", "коммерческое предложение")):
                    metrics["potential_deals"] += 1
                # Findings are intentionally not materialized here. Initial,
                # incremental and scheduled ingestion all converge on the same
                # Signal -> correlation -> Problem lifecycle below.
            run.metrics_json = metrics
            run.status = "completed"
            run.stage = "completed"
            run.progress_percent = 100
            run.finished_at = datetime.now(UTC)
            connection.status = "ready"
            connection.progress_stage = "completed"
            connection.progress_percent = 100
            connection.progress_json = {**connection.progress_json, **metrics}
            connection.last_sync_at = datetime.now(UTC)

        await self.transactions.run(write)
        async with self.session_factory() as session:
            run = await session.get(InitialAnalysisRun, run_id)
            message_ids = list(
                await session.scalars(
                    select(TelegramMessage.id)
                    .where(
                        TelegramMessage.tenant_id == run.tenant_id,
                        TelegramMessage.connection_id == run.connection_id,
                        TelegramMessage.sent_at
                        >= datetime.now(UTC) - timedelta(days=run.history_days),
                    )
                    .order_by(TelegramMessage.sent_at.asc())
                )
            )
        batches = [message_ids[index : index + 100] for index in range(0, len(message_ids), 100)]
        for index, batch in enumerate(batches):
            await self.queue.enqueue(
                "signal.scan_batch",
                {"message_ids": batch, "source": "initial_sync"},
                tenant_id=run.tenant_id,
                telegram_account_id=run.connection_id,
                priority=JOB_PRIORITY["P3"],
                idempotency_key=f"signal-history-batch:{run.id}:{index}",
                correlation_id=run.id,
                category="historical",
                cost_class="light",
                max_attempts=3,
            )

        async def update_queued_count(session: AsyncSession) -> None:
            current = await session.get(InitialAnalysisRun, run_id)
            current.metrics_json = {
                **(current.metrics_json or {}),
                "analysis_jobs_queued": len(batches),
            }

        await self.transactions.run(update_queued_count)
