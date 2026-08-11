from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import SQLiteTransactionManager
from ..jobs.queue import SQLiteJobQueue
from ..models import (
    BackgroundJob,
    Employee,
    EncryptedSecret,
    InitialAnalysisRun,
    OperationalProblem,
    TelegramConnection,
    TelegramDialog,
    TelegramFolder,
    TelegramMessage,
    TelegramSyncCursor,
    TenantSettings,
)
from ..services.encryption import EncryptionService
from .gateway import (
    LoginChallenge,
    LoginResult,
    TelegramSessionRevoked,
    TelegramUserGateway,
    TelethonGateway,
)

logger = logging.getLogger(__name__)
LOGIN_RESEND_COOLDOWN_SECONDS = 30


class TelegramConnectionError(RuntimeError):
    pass


def mask_phone(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) < 7:
        raise ValueError("phone number is invalid")
    return f"+{digits[:2]}***{digits[-4:]}"


class TelegramConnectionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        gateway: TelegramUserGateway,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.gateway = gateway
        self.transactions = SQLiteTransactionManager(session_factory)
        self.queue = SQLiteJobQueue(session_factory)

    def _secret(self, tenant_id: str, kind: str, value: str) -> EncryptedSecret:
        return EncryptedSecret(
            tenant_id=tenant_id,
            kind=kind,
            ciphertext=self.encryption.encrypt(value),
            fingerprint=self.encryption.fingerprint(value),
        )

    async def _replace_secret(
        self, session: AsyncSession, tenant_id: str, kind: str, value: str
    ) -> EncryptedSecret:
        fingerprint = self.encryption.fingerprint(value)
        secret = await session.scalar(
            select(EncryptedSecret).where(
                EncryptedSecret.tenant_id == tenant_id,
                EncryptedSecret.kind == kind,
                EncryptedSecret.fingerprint == fingerprint,
            )
        )
        if secret is None:
            secret = self._secret(tenant_id, kind, value)
            session.add(secret)
        else:
            secret.ciphertext = self.encryption.encrypt(value)
            secret.deleted_at = None
        await session.flush()
        return secret

    async def get(
        self, tenant_id: str, connection_id: str | None = None
    ) -> TelegramConnection | None:
        async with self.session_factory() as session:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            return await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )

    async def get_all(self, tenant_id: str) -> list[TelegramConnection]:
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(TelegramConnection)
                    .where(
                        TelegramConnection.tenant_id == tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                    )
                    .order_by(TelegramConnection.created_at.asc())
                )
            )

    async def begin_login(
        self,
        tenant_id: str,
        phone: str,
        assigned_employee_id: str | None = None,
    ) -> TelegramConnection:
        masked = mask_phone(phone)
        challenge = await self.gateway.begin_login(phone)

        async def write(session: AsyncSession) -> str:
            if assigned_employee_id and not await session.scalar(
                select(Employee.id).where(
                    Employee.id == assigned_employee_id,
                    Employee.tenant_id == tenant_id,
                )
            ):
                raise LookupError("employee does not belong to tenant")
            connection = TelegramConnection(
                tenant_id=tenant_id,
                assigned_employee_id=assigned_employee_id,
            )
            session.add(connection)
            await session.flush()
            pending = await self._replace_secret(
                session, tenant_id, "telegram_pending_session", challenge.session_string
            )
            phone_secret = await self._replace_secret(session, tenant_id, "telegram_phone", phone)
            code_hash = await self._replace_secret(
                session, tenant_id, "telegram_phone_code_hash", challenge.phone_code_hash
            )
            connection.pending_session_secret_id = pending.id
            connection.phone_secret_id = phone_secret.id
            connection.phone_code_hash_secret_id = code_hash.id
            connection.phone_masked = masked
            connection.status = "awaiting_code"
            connection.progress_json = self._code_delivery_progress(challenge)
            connection.consent_at = datetime.now(UTC)
            connection.consent_version = "2026-08-04"
            connection.last_error_code = None
            return connection.id

        connection_id = await self.transactions.run(write)
        logger.info(
            "Telegram login code accepted tenant_id=%s connection_id=%s delivery_type=%s",
            tenant_id,
            connection_id,
            challenge.delivery_type,
        )
        async with self.session_factory() as session:
            return await session.get(TelegramConnection, connection_id)

    async def resend_login(self, tenant_id: str, connection_id: str) -> TelegramConnection:
        async with self.session_factory() as session:
            connection = await session.scalar(
                select(TelegramConnection).where(
                    TelegramConnection.id == connection_id,
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                    TelegramConnection.status == "awaiting_code",
                )
            )
            if connection is None or not all(
                (
                    connection.pending_session_secret_id,
                    connection.phone_secret_id,
                    connection.phone_code_hash_secret_id,
                )
            ):
                raise TelegramConnectionError("login challenge is missing")
            remaining = self.resend_available_in(connection)
            if remaining > 0:
                raise TelegramConnectionError(f"resend cooldown:{remaining}")
            pending = await session.get(EncryptedSecret, connection.pending_session_secret_id)
            phone = await session.get(EncryptedSecret, connection.phone_secret_id)
            code_hash = await session.get(EncryptedSecret, connection.phone_code_hash_secret_id)
            if pending is None or phone is None or code_hash is None:
                raise TelegramConnectionError("login challenge secrets are missing")
            session_string = self.encryption.decrypt(pending.ciphertext)
            phone_value = self.encryption.decrypt(phone.ciphertext)
            hash_value = self.encryption.decrypt(code_hash.ciphertext)

        challenge = await self.gateway.resend_login(session_string, phone_value, hash_value)
        session_string = phone_value = hash_value = ""

        async def write(session: AsyncSession) -> str:
            connection = await session.scalar(
                select(TelegramConnection).where(
                    TelegramConnection.id == connection_id,
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                    TelegramConnection.status == "awaiting_code",
                )
            )
            if connection is None:
                raise TelegramConnectionError("login challenge disappeared")
            pending_secret = await self._replace_secret(
                session, tenant_id, "telegram_pending_session", challenge.session_string
            )
            code_hash_secret = await self._replace_secret(
                session, tenant_id, "telegram_phone_code_hash", challenge.phone_code_hash
            )
            for old_id, new_id in (
                (connection.pending_session_secret_id, pending_secret.id),
                (connection.phone_code_hash_secret_id, code_hash_secret.id),
            ):
                if old_id and old_id != new_id:
                    old = await session.get(EncryptedSecret, old_id)
                    if old:
                        old.deleted_at = datetime.now(UTC)
            connection.pending_session_secret_id = pending_secret.id
            connection.phone_code_hash_secret_id = code_hash_secret.id
            connection.progress_json = self._code_delivery_progress(challenge)
            connection.last_error_code = None
            return connection.id

        target_id = await self.transactions.run(write)
        logger.info(
            "Telegram login code resent tenant_id=%s connection_id=%s delivery_type=%s",
            tenant_id,
            target_id,
            challenge.delivery_type,
        )
        async with self.session_factory() as session:
            return await session.get(TelegramConnection, target_id)

    @staticmethod
    def _code_delivery_progress(challenge: LoginChallenge) -> dict[str, object]:
        sent_at = datetime.now(UTC)
        return {
            "login_code": {
                "sent_at": sent_at.isoformat(),
                "resend_available_at": (
                    sent_at + timedelta(seconds=LOGIN_RESEND_COOLDOWN_SECONDS)
                ).isoformat(),
                "delivery_type": challenge.delivery_type,
                "next_delivery_type": challenge.next_delivery_type,
                "telegram_timeout_seconds": challenge.timeout_seconds,
            }
        }

    @staticmethod
    def resend_available_in(connection: TelegramConnection) -> int:
        metadata = (connection.progress_json or {}).get("login_code", {})
        raw_value = metadata.get("resend_available_at") if isinstance(metadata, dict) else None
        if not isinstance(raw_value, str):
            return 0
        try:
            available_at = datetime.fromisoformat(raw_value)
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
        except ValueError:
            return 0
        remaining = (available_at - datetime.now(UTC)).total_seconds()
        return max(0, int(remaining + 0.999))

    async def complete_login(
        self,
        tenant_id: str,
        *,
        connection_id: str | None = None,
        code: str | None = None,
        password: str | None = None,
    ) -> TelegramConnection:
        async with self.session_factory() as session:
            query = (
                select(TelegramConnection)
                .where(
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                    TelegramConnection.status.in_(("awaiting_code", "awaiting_2fa")),
                )
                .order_by(TelegramConnection.created_at.desc())
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(query.limit(1))
            if connection is None or not all(
                (
                    connection.pending_session_secret_id,
                    connection.phone_secret_id,
                    connection.phone_code_hash_secret_id,
                )
            ):
                raise TelegramConnectionError("login challenge is missing")
            pending = await session.get(EncryptedSecret, connection.pending_session_secret_id)
            phone = await session.get(EncryptedSecret, connection.phone_secret_id)
            code_hash = await session.get(EncryptedSecret, connection.phone_code_hash_secret_id)
            session_string = self.encryption.decrypt(pending.ciphertext)
            phone_value = self.encryption.decrypt(phone.ciphertext)
            hash_value = self.encryption.decrypt(code_hash.ciphertext)
            connection_id = connection.id
        result = await self.gateway.complete_login(
            session_string,
            phone_value,
            hash_value,
            code=code,
            password=password,
        )
        session_string = phone_value = hash_value = ""
        return await self._store_login_result(connection_id, result)

    async def _store_login_result(
        self, connection_id: str, result: LoginResult
    ) -> TelegramConnection:
        async def write(session: AsyncSession) -> str:
            connection = await session.get(TelegramConnection, connection_id)
            if connection is None:
                raise TelegramConnectionError("connection disappeared")
            pending = await self._replace_secret(
                session,
                connection.tenant_id,
                "telegram_pending_session",
                result.session_string,
            )
            old_pending = connection.pending_session_secret_id
            connection.pending_session_secret_id = pending.id
            if old_pending:
                old = await session.get(EncryptedSecret, old_pending)
                if old:
                    old.deleted_at = datetime.now(UTC)
            if result.status == "awaiting_2fa":
                connection.status = "awaiting_2fa"
                return connection.id
            existing = await session.scalar(
                select(TelegramConnection).where(
                    TelegramConnection.tenant_id == connection.tenant_id,
                    TelegramConnection.telegram_user_id == result.telegram_user_id,
                    TelegramConnection.id != connection.id,
                    TelegramConnection.deleted_at.is_(None),
                )
            )
            permanent = await self._replace_secret(
                session,
                connection.tenant_id,
                "telegram_user_session",
                result.session_string,
            )
            target = existing or connection
            target.session_secret_id = permanent.id
            target.telegram_user_id = result.telegram_user_id
            target.username = result.username
            target.display_name = result.display_name
            target.status = "connected"
            target.progress_stage = "account_connected"
            target.progress_percent = 10
            for ephemeral_id in (
                connection.pending_session_secret_id,
                connection.phone_secret_id,
                connection.phone_code_hash_secret_id,
            ):
                if ephemeral_id:
                    ephemeral = await session.get(EncryptedSecret, ephemeral_id)
                    if ephemeral and ephemeral.id != permanent.id:
                        ephemeral.deleted_at = datetime.now(UTC)
            connection.pending_session_secret_id = None
            connection.phone_secret_id = None
            connection.phone_code_hash_secret_id = None
            if existing is not None:
                await session.delete(connection)
            return target.id

        target_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            return await session.get(TelegramConnection, target_id)

    async def refresh_catalog(
        self, tenant_id: str, connection_id: str | None = None
    ) -> TelegramConnection:
        if not isinstance(self.gateway, TelethonGateway):
            return await self._legacy_refresh_catalog(tenant_id, connection_id)
        connection = await self.get(tenant_id, connection_id)
        if connection is None or connection.session_secret_id is None:
            raise TelegramConnectionError("connected session is required")
        job_id = await self.queue.enqueue(
            "telegram.refresh_catalog",
            {},
            tenant_id=tenant_id,
            telegram_account_id=connection.id,
            category="telegram_rpc",
            idempotency_key=(f"telegram-catalog:{connection.id}:{datetime.now(UTC):%Y%m%d%H%M%S}"),
            max_attempts=5,
        )
        await self._wait_for_runtime_job(job_id)
        refreshed = await self.get(tenant_id, connection.id)
        if refreshed is None:
            raise TelegramConnectionError("connection disappeared")
        return refreshed

    async def _legacy_refresh_catalog(
        self, tenant_id: str, connection_id: str | None = None
    ) -> TelegramConnection:
        """Deprecated test adapter; production catalog RPC is owned by TelegramSessionActor."""
        connection, session_string = await self.connection_session(tenant_id, connection_id)
        folders = await self.gateway.list_folders(session_string)
        dialogs = await self.gateway.list_dialogs(session_string)
        session_string = ""

        async def write(session: AsyncSession) -> None:
            await session.execute(
                delete(TelegramFolder).where(TelegramFolder.connection_id == connection.id)
            )
            for folder in folders:
                session.add(
                    TelegramFolder(
                        tenant_id=tenant_id,
                        connection_id=connection.id,
                        telegram_folder_id=folder.id,
                        title=folder.title,
                        chat_count=sum(1 for dialog in dialogs if dialog.folder_id == folder.id),
                    )
                )
            existing = {
                row.telegram_dialog_id: row
                for row in await session.scalars(
                    select(TelegramDialog).where(TelegramDialog.connection_id == connection.id)
                )
            }
            for remote in dialogs:
                row = existing.get(remote.id) or TelegramDialog(
                    tenant_id=tenant_id,
                    connection_id=connection.id,
                    telegram_dialog_id=remote.id,
                    canonical_peer_id=str(remote.id),
                )
                row.canonical_peer_id = str(remote.id)
                row.title = remote.title
                row.username = remote.username
                row.dialog_type = remote.dialog_type
                row.folder_id = remote.folder_id
                row.source = "personal" if remote.dialog_type == "personal" else "folder"
                row.participants_count = remote.participants_count
                row.last_message_at = remote.last_message_at
                if remote.dialog_type == "personal":
                    row.classification = "auto_personal"
                    row.confidence = 1.0
                    row.requires_user_confirmation = False
                    row.selected = True
                    row.excluded = False
                elif row.id is None:
                    row.selected = False
                    row.excluded = False
                if row.id is None:
                    session.add(row)
            current = await session.get(TelegramConnection, connection.id)
            current.progress_stage = "catalog_ready"
            current.progress_percent = 25
            current.progress_json = {
                "folders": len(folders),
                "dialogs": len(dialogs),
                "personal_dialogs": sum(1 for item in dialogs if item.dialog_type == "personal"),
            }

        await self.transactions.run(write)
        return await self.get(tenant_id, connection.id)

    async def _wait_for_runtime_job(self, job_id: str, timeout_seconds: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            job = await self.queue.get(job_id)
            if job is not None and job.status == "completed":
                return
            if job is not None and job.status in {"failed", "cancelled"}:
                raise TelegramConnectionError(f"Telegram runtime RPC failed: {job.status}")
            await asyncio.sleep(0.1)
        raise TelegramConnectionError("Telegram runtime RPC timed out")

    async def activate_default_scope(
        self,
        tenant_id: str,
        *,
        history_days: int = 7,
        connection_id: str | None = None,
    ) -> TelegramConnection:
        if not 0 <= history_days <= 180:
            raise ValueError("history_days must be between 0 and 180")

        async def write(session: AsyncSession) -> None:
            connection = await session.scalar(
                select(TelegramConnection).where(
                    TelegramConnection.id == connection_id,
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                )
            )
            if connection is None:
                raise TelegramConnectionError("connected session is required")
            connection.personal_dialogs_consent = True
            connection.history_days = history_days
            connection.selected_folder_id = None
            connection.selected_folder_ids = []
            connection.selected_folder_title = None
            connection.progress_stage = "personal_sources_enabled"
            connection.progress_percent = 30
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
            )
            active_days = settings.active_dialog_days if settings else 30
            active_cutoff = datetime.now(UTC) - timedelta(days=active_days)
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(TelegramDialog.connection_id == connection.id)
                )
            )
            for dialog in dialogs:
                if dialog.dialog_type == "personal":
                    dialog.selected = bool(
                        dialog.last_message_at is not None
                        and dialog.last_message_at >= active_cutoff
                    )
                    dialog.excluded = False
                    dialog.classification = "auto_personal"
                    dialog.requires_user_confirmation = False
                else:
                    dialog.selected = False

        await self.transactions.run(write)
        result = await self.get(tenant_id, connection_id)
        if result is None:
            raise TelegramConnectionError("connection disappeared")
        return result

    async def list_folders(
        self, tenant_id: str, connection_id: str | None = None
    ) -> list[TelegramFolder]:
        connection = await self.get(tenant_id, connection_id)
        if connection is None:
            return []
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(TelegramFolder)
                    .where(TelegramFolder.connection_id == connection.id)
                    .order_by(TelegramFolder.title)
                )
            )

    async def select_scope(
        self,
        tenant_id: str,
        folder_id: int | list[int],
        *,
        personal_dialogs_consent: bool,
        history_days: int = 7,
        connection_id: str | None = None,
    ) -> TelegramConnection:
        if not 0 <= history_days <= 180:
            raise ValueError("history_days must be between 0 and 180")
        folder_ids = [folder_id] if isinstance(folder_id, int) else list(dict.fromkeys(folder_id))
        if not folder_ids:
            raise ValueError("at least one working folder is required")

        async def write(session: AsyncSession) -> None:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            folders = list(
                await session.scalars(
                    select(TelegramFolder).where(
                        TelegramFolder.connection_id == connection.id,
                        TelegramFolder.telegram_folder_id.in_(folder_ids),
                    )
                )
            )
            if len(folders) != len(folder_ids):
                raise LookupError("folder does not belong to connection")
            by_id = {item.telegram_folder_id: item for item in folders}
            connection.selected_folder_id = folder_ids[0]
            connection.selected_folder_ids = folder_ids
            connection.selected_folder_title = ", ".join(by_id[item].title for item in folder_ids)
            # Legacy folder configuration may still call this endpoint. The current
            # source policy always monitors personal dialogs; groups remain opt-in.
            connection.personal_dialogs_consent = True
            connection.history_days = history_days
            connection.progress_stage = "scope_selected"
            connection.progress_percent = 30
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(TelegramDialog.connection_id == connection.id)
                )
            )
            for dialog in dialogs:
                in_work_folder = dialog.folder_id in folder_ids
                personal_candidate = dialog.dialog_type == "personal"
                dialog.selected = (in_work_folder or personal_candidate) and not dialog.excluded
                if personal_candidate:
                    dialog.requires_user_confirmation = False

        await self.transactions.run(write)
        result = await self.get(tenant_id, connection_id)
        if result is None:
            raise TelegramConnectionError("connection disappeared")
        return result

    async def start_initial_sync(
        self,
        tenant_id: str,
        *,
        progress_chat_id: int | None = None,
        progress_message_id: int | None = None,
        connection_id: str | None = None,
    ) -> InitialAnalysisRun:
        async def write(session: AsyncSession) -> str:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            if connection is None or connection.session_secret_id is None:
                raise TelegramConnectionError("connected session is required")
            active = await session.scalar(
                select(InitialAnalysisRun).where(
                    InitialAnalysisRun.connection_id == connection.id,
                    InitialAnalysisRun.status.in_(("pending", "running")),
                )
            )
            if active is not None and not active.stop_requested:
                return active.id
            selected = list(
                await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.connection_id == connection.id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            )
            if not selected:
                raise TelegramConnectionError("select at least one working dialog")
            generation = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(InitialAnalysisRun.generation), 0)).where(
                            InitialAnalysisRun.connection_id == connection.id
                        )
                    )
                )
                + 1
            )
            run = InitialAnalysisRun(
                tenant_id=tenant_id,
                connection_id=connection.id,
                generation=generation,
                status="running",
                stage="history_sync",
                history_days=connection.history_days,
                progress_percent=30,
                total_dialogs=len(selected),
                started_at=datetime.now(UTC),
                progress_chat_id=progress_chat_id,
                progress_message_id=progress_message_id,
            )
            session.add(run)
            await session.flush()
            for dialog in selected:
                session.add(
                    TelegramSyncCursor(
                        tenant_id=tenant_id,
                        run_id=run.id,
                        connection_id=connection.id,
                        dialog_id=dialog.id,
                    )
                )
            connection.status = "syncing"
            connection.stop_requested = False
            connection.progress_stage = "history_sync"
            connection.progress_percent = 30
            return run.id

        run_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            current_run = await session.get(InitialAnalysisRun, run_id)
            cursor_ids = list(
                await session.scalars(
                    select(TelegramSyncCursor.id).where(TelegramSyncCursor.run_id == run_id)
                )
            )
        for cursor_id in cursor_ids:
            await self.queue.enqueue(
                "telegram.sync_chat",
                {"cursor_id": cursor_id},
                tenant_id=tenant_id,
                telegram_account_id=current_run.connection_id,
                idempotency_key=f"telegram-sync:{cursor_id}:0",
                category="telegram_rpc",
                cost_class="light",
                max_attempts=8,
            )
        async with self.session_factory() as session:
            return await session.get(InitialAnalysisRun, run_id)

    async def exclude_dialog(self, tenant_id: str, dialog_id: str) -> None:
        async def write(session: AsyncSession) -> None:
            dialog = await session.scalar(
                select(TelegramDialog).where(
                    TelegramDialog.id == dialog_id, TelegramDialog.tenant_id == tenant_id
                )
            )
            if dialog is None:
                raise LookupError("dialog not found in tenant")
            dialog.excluded = True
            dialog.selected = False

        await self.transactions.run(write)

    async def list_personal_candidates(self, tenant_id: str) -> list[TelegramDialog]:
        connection = await self.get(tenant_id)
        if connection is None:
            return []
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(TelegramDialog)
                    .where(
                        TelegramDialog.connection_id == connection.id,
                        TelegramDialog.dialog_type == "personal",
                        TelegramDialog.excluded.is_(False),
                    )
                    .order_by(
                        TelegramDialog.requires_user_confirmation.desc(),
                        TelegramDialog.confidence.desc(),
                    )
                    .limit(30)
                )
            )

    async def confirm_personal_dialog(
        self, tenant_id: str, dialog_id: str, *, include: bool
    ) -> None:
        async def write(session: AsyncSession) -> None:
            dialog = await session.scalar(
                select(TelegramDialog).where(
                    TelegramDialog.id == dialog_id,
                    TelegramDialog.tenant_id == tenant_id,
                    TelegramDialog.dialog_type == "personal",
                )
            )
            if dialog is None:
                raise LookupError("personal dialog not found in tenant")
            dialog.requires_user_confirmation = False
            dialog.classification = "confirmed_client" if include else "personal_excluded"
            dialog.selected = include
            dialog.excluded = not include

        await self.transactions.run(write)

    async def stop_run(self, tenant_id: str, run_id: str) -> None:
        async def write(session: AsyncSession) -> None:
            run = await session.scalar(
                select(InitialAnalysisRun).where(
                    InitialAnalysisRun.id == run_id,
                    InitialAnalysisRun.tenant_id == tenant_id,
                )
            )
            if run is None:
                raise LookupError("analysis run not found in tenant")
            run.stop_requested = True
            run.stage = "stopping"
            connection = await session.get(TelegramConnection, run.connection_id)
            connection.stop_requested = True
            connection.progress_stage = "stopping"

        await self.transactions.run(write)

    async def disconnect(self, tenant_id: str, connection_id: str | None = None) -> None:
        target = await self.get(tenant_id, connection_id)
        if target is not None:
            connection_id = target.id
        if (
            target is not None
            and target.session_secret_id
            and isinstance(self.gateway, TelethonGateway)
        ):
            job_id = await self.queue.enqueue(
                "telegram.logout",
                {},
                tenant_id=tenant_id,
                telegram_account_id=target.id,
                category="telegram_rpc",
                idempotency_key=f"telegram-logout:{target.id}",
                max_attempts=2,
            )
            try:
                await self._wait_for_runtime_job(job_id)
            except Exception as exc:  # noqa: BLE001 - local removal must still complete
                logger.warning("Remote Telegram logout failed (%s)", type(exc).__name__)
        elif target is not None and target.session_secret_id:
            _, session_string = await self.connection_session(tenant_id, target.id)
            try:
                await self.gateway.terminate_session(session_string)
            except Exception as exc:  # noqa: BLE001 - test adapter logout is best effort
                logger.warning("Remote Telegram logout failed (%s)", type(exc).__name__)

        async def write(session: AsyncSession) -> None:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            if connection is None:
                return
            for secret_id in (
                connection.session_secret_id,
                connection.pending_session_secret_id,
                connection.phone_secret_id,
                connection.phone_code_hash_secret_id,
            ):
                if secret_id:
                    secret = await session.get(EncryptedSecret, secret_id)
                    if secret:
                        secret.deleted_at = datetime.now(UTC)
            connection.session_secret_id = None
            connection.pending_session_secret_id = None
            connection.phone_secret_id = None
            connection.phone_code_hash_secret_id = None
            connection.status = "disconnected"
            connection.stop_requested = True
            connection.progress_stage = "disconnected"
            await session.execute(
                update(InitialAnalysisRun)
                .where(
                    InitialAnalysisRun.connection_id == connection.id,
                    InitialAnalysisRun.status.in_(("pending", "running")),
                )
                .values(stop_requested=True, stage="stopping")
            )

        await self.transactions.run(write)

    async def cancel_login(self, tenant_id: str, connection_id: str | None = None) -> None:
        async def write(session: AsyncSession) -> None:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            if connection is None:
                return
            for attribute in (
                "pending_session_secret_id",
                "phone_secret_id",
                "phone_code_hash_secret_id",
            ):
                secret_id = getattr(connection, attribute)
                if secret_id:
                    secret = await session.get(EncryptedSecret, secret_id)
                    if secret:
                        secret.deleted_at = datetime.now(UTC)
                    setattr(connection, attribute, None)
            if connection.session_secret_id is None:
                connection.status = "disconnected"
                connection.progress_stage = "not_started"

        await self.transactions.run(write)

    async def check_health(
        self, tenant_id: str, connection_id: str | None = None
    ) -> TelegramConnection:
        connection = await self.get(tenant_id, connection_id)
        if connection is None or connection.session_secret_id is None:
            raise TelegramConnectionError("connected session is required")
        if isinstance(self.gateway, TelethonGateway):
            job_id = await self.queue.enqueue(
                "telegram.health",
                {},
                tenant_id=tenant_id,
                telegram_account_id=connection.id,
                category="telegram_rpc",
                idempotency_key=(f"telegram-health:{connection.id}:{datetime.now(UTC):%Y%m%d%H%M}"),
                max_attempts=3,
            )
            await self._wait_for_runtime_job(job_id)
            result = await self.get(tenant_id, connection.id)
            if result is None:
                raise TelegramConnectionError("connection disappeared")
            return result
        return await self._legacy_check_health(tenant_id, connection_id)

    async def _legacy_check_health(
        self, tenant_id: str, connection_id: str | None = None
    ) -> TelegramConnection:
        connection, session_string = await self.connection_session(tenant_id, connection_id)
        status = "healthy"
        safe_error = None
        try:
            profile = await self.gateway.check_session(session_string)
        except TelegramSessionRevoked:
            status = "revoked"
            safe_error = "session_revoked"
            profile = None
        except Exception as exc:  # noqa: BLE001 - provider details stay out of storage
            status = "unavailable"
            safe_error = type(exc).__name__
            profile = None
        finally:
            session_string = ""

        async def write(session: AsyncSession) -> None:
            current = await session.get(TelegramConnection, connection.id)
            current.last_health_check_at = datetime.now(UTC)
            current.health_status = status
            current.last_error_code = safe_error
            if status == "revoked":
                current.status = "reauthorization_required"
            if profile:
                current.telegram_user_id = profile.telegram_user_id
                current.username = profile.username
                current.display_name = profile.display_name

        await self.transactions.run(write)
        result = await self.get(tenant_id, connection.id)
        if result is None:
            raise TelegramConnectionError("connection disappeared")
        return result

    async def clear_data(self, tenant_id: str, connection_id: str | None = None) -> None:
        async def write(session: AsyncSession) -> None:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            if connection is None:
                return
            await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.tenant_id == tenant_id,
                    BackgroundJob.job_type == "telegram.sync_chat",
                    BackgroundJob.status.in_(("pending", "retry_scheduled")),
                )
                .values(status="cancelled", finished_at=datetime.now(UTC))
            )
            await session.execute(
                delete(OperationalProblem).where(OperationalProblem.connection_id == connection.id)
            )
            await session.execute(
                delete(TelegramMessage).where(TelegramMessage.connection_id == connection.id)
            )
            await session.execute(
                delete(TelegramSyncCursor).where(TelegramSyncCursor.connection_id == connection.id)
            )
            await session.execute(
                delete(InitialAnalysisRun).where(InitialAnalysisRun.connection_id == connection.id)
            )
            await session.execute(
                delete(TelegramDialog).where(TelegramDialog.connection_id == connection.id)
            )
            await session.execute(
                delete(TelegramFolder).where(TelegramFolder.connection_id == connection.id)
            )
            await session.delete(connection)
            await session.flush()
            remaining = list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                    )
                )
            )
            referenced = {
                secret_id
                for item in remaining
                for secret_id in (
                    item.session_secret_id,
                    item.pending_session_secret_id,
                    item.phone_secret_id,
                    item.phone_code_hash_secret_id,
                )
                if secret_id
            }
            secret_query = select(EncryptedSecret).where(
                EncryptedSecret.tenant_id == tenant_id,
                EncryptedSecret.kind.like("telegram_%"),
            )
            if referenced:
                secret_query = secret_query.where(EncryptedSecret.id.not_in(referenced))
            telegram_secrets = list(await session.scalars(secret_query))
            for secret in telegram_secrets:
                await session.delete(secret)

        await self.transactions.run(write)

    async def connection_session(
        self, tenant_id: str, connection_id: str | None = None
    ) -> tuple[TelegramConnection, str]:
        async with self.session_factory() as session:
            query = select(TelegramConnection).where(
                TelegramConnection.tenant_id == tenant_id,
                TelegramConnection.deleted_at.is_(None),
            )
            if connection_id is not None:
                query = query.where(TelegramConnection.id == connection_id)
            connection = await session.scalar(
                query.order_by(TelegramConnection.created_at.desc()).limit(1)
            )
            if connection is None or connection.session_secret_id is None:
                raise TelegramConnectionError("connected session is required")
            secret = await session.get(EncryptedSecret, connection.session_secret_id)
            return connection, self.encryption.decrypt(secret.ciphertext)

    async def _connection_session(self, tenant_id: str) -> tuple[TelegramConnection, str]:
        return await self.connection_session(tenant_id)
