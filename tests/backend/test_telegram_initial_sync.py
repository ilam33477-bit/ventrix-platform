from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.signals import SignalService
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.jobs.worker import BackgroundWorker
from services.backend.models import (
    BackgroundJob,
    EncryptedSecret,
    InitialAnalysisRun,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
)
from services.backend.services.encryption import EncryptionService
from services.backend.telegram_sessions.gateway import (
    LoginChallenge,
    LoginResult,
    MessageBatch,
    RemoteDialog,
    RemoteFolder,
    RemoteMessage,
)
from services.backend.telegram_sessions.service import (
    TelegramConnectionError,
    TelegramConnectionService,
    normalize_phone_number,
)
from services.backend.telegram_sessions.sync import TelegramSyncHandlers


class FakeTelegramGateway:
    def __init__(self, *, require_2fa: bool = False) -> None:
        self.require_2fa = require_2fa
        self.fetch_calls: list[tuple[int, int]] = []
        self.terminated_sessions: list[str] = []
        self.resend_calls = 0
        self.requested_phones: list[str] = []
        self.cancelled_logins: list[str] = []

    async def begin_login(self, phone: str) -> LoginChallenge:
        assert phone.startswith("+")
        self.requested_phones.append(phone)
        return LoginChallenge("pending-session-a", "phone-code-hash", "telegram_app")

    async def resend_login(
        self, session_string: str, phone: str, phone_code_hash: str
    ) -> LoginChallenge:
        assert session_string == "pending-session-a"
        assert phone_code_hash == "phone-code-hash"
        self.resend_calls += 1
        return LoginChallenge("pending-session-resend", "phone-code-hash-2", "sms")

    async def cancel_login(self, session_string: str) -> None:
        self.cancelled_logins.append(session_string)

    async def complete_login(
        self,
        session_string: str,
        phone: str,
        phone_code_hash: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> LoginResult:
        if self.require_2fa and password is None:
            return LoginResult("awaiting_2fa", "pending-session-b")
        return LoginResult("connected", "authorized-session", 777001, "work_owner", "Work Owner")

    async def list_folders(self, session_string: str) -> list[RemoteFolder]:
        assert session_string == "authorized-session"
        return [RemoteFolder(10, "Работа")]

    async def list_dialogs(self, session_string: str) -> list[RemoteDialog]:
        return [
            RemoteDialog(1001, "Client Group", "client_group", "group", 10),
            RemoteDialog(1002, "Private Friend", "friend", "personal", None),
        ]

    async def fetch_messages(
        self,
        session_string: str,
        dialog_id: int,
        *,
        offset_id: int,
        limit: int,
    ) -> MessageBatch:
        self.fetch_calls.append((dialog_id, offset_id))
        if offset_id:
            return MessageBatch([], offset_id, False)
        message = RemoteMessage(
            501,
            9001,
            "customer",
            datetime.now(UTC) - timedelta(hours=2),
            None,
            False,
            "Когда вы пришлёте коммерческое предложение?",
            [
                {
                    "kind": "MessageMediaDocument",
                    "name": "brief.pdf",
                    "size": 100,
                    "mime_type": "application/pdf",
                }
            ],
        )
        return MessageBatch([message], 501, False)

    async def terminate_session(self, session_string: str) -> None:
        self.terminated_sessions.append(session_string)


async def connected_service(
    session_factory, make_service, tenant_payload, encryption_key, *, require_2fa=False
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    gateway = FakeTelegramGateway(require_2fa=require_2fa)
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    await service.begin_login(tenant.id, "+79990001122")
    connection = await service.complete_login(tenant.id, code="12345")
    if require_2fa:
        assert connection.status == "awaiting_2fa"
        connection = await service.complete_login(tenant.id, password="not-stored")
    assert connection.status == "connected"
    return tenant, gateway, service


@pytest.mark.asyncio
async def test_login_2fa_and_session_are_encrypted_without_secret_leaks(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant, _, _ = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key, require_2fa=True
    )
    async with session_factory() as session:
        connection = await session.scalar(
            select(TelegramConnection).where(TelegramConnection.tenant_id == tenant.id)
        )
        secrets = list(
            await session.scalars(
                select(EncryptedSecret).where(EncryptedSecret.tenant_id == tenant.id)
            )
        )
    combined = b" ".join(secret.ciphertext for secret in secrets)
    assert b"authorized-session" not in combined
    assert b"12345" not in combined
    assert b"not-stored" not in combined
    assert connection.phone_masked.endswith("1122") and "999000" not in connection.phone_masked


@pytest.mark.asyncio
async def test_login_code_resend_is_rate_limited_and_rotates_challenge(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    gateway = FakeTelegramGateway()
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    connection = await service.begin_login(tenant.id, "+79990001122")

    with pytest.raises(TelegramConnectionError, match="resend cooldown"):
        await service.resend_login(tenant.id, connection.id)

    async with session_factory() as session:
        row = await session.get(TelegramConnection, connection.id)
        metadata = dict(row.progress_json)
        metadata["login_code"] = {
            **metadata["login_code"],
            "resend_available_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        row.progress_json = metadata
        await session.commit()

    resent = await service.resend_login(tenant.id, connection.id)
    assert resent.id == connection.id
    assert resent.status == "awaiting_code"
    assert resent.progress_json["login_code"]["delivery_type"] == "sms"
    assert gateway.resend_calls == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+7 909 941-20-79", "+79099412079"),
        ("8 (909) 941-20-79", "+79099412079"),
        ("9099412079", "+79099412079"),
        ("0044 7700 900123", "+447700900123"),
    ],
)
def test_phone_normalization_supports_russian_and_e164_inputs(value, expected) -> None:
    assert normalize_phone_number(value) == expected


@pytest.mark.asyncio
async def test_new_login_supersedes_previous_pending_challenge_for_same_scope(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    gateway = FakeTelegramGateway()
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)

    first = await service.begin_login(tenant.id, "8 999 000-11-22")
    second = await service.begin_login(tenant.id, "+7 909 941-20-79")

    async with session_factory() as session:
        first = await session.get(TelegramConnection, first.id)
        second = await session.get(TelegramConnection, second.id)
    assert first.status == "disconnected"
    assert first.last_error_code == "login_superseded"
    assert first.pending_session_secret_id is None
    assert first.phone_code_hash_secret_id is None
    assert second.status == "awaiting_code"
    assert gateway.requested_phones == ["+79990001122", "+79099412079"]
    assert gateway.cancelled_logins == ["pending-session-a"]


@pytest.mark.asyncio
async def test_folder_scope_resumable_batches_problems_and_personal_consent(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant, gateway, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    await service.refresh_catalog(tenant.id)
    folders = await service.list_folders(tenant.id)
    assert [(folder.telegram_folder_id, folder.title) for folder in folders] == [(10, "Работа")]
    await service.select_scope(tenant.id, 10, personal_dialogs_consent=False, history_days=7)
    run = await service.start_initial_sync(tenant.id)

    sync = TelegramSyncHandlers(
        session_factory,
        EncryptionService(encryption_key),
        gateway,
        batch_size=10,
        batch_pause_seconds=0.001,
    )
    queue = SQLiteJobQueue(session_factory)
    signal_service = SignalService(session_factory, queue)
    worker = BackgroundWorker(
        queue,
        "sync-worker",
        {
            "telegram.sync_chat": sync.sync_chat,
            "signal.local_scan": signal_service.local_scan_job,
            "signal.scan_batch": signal_service.scan_batch_job,
        },
    )
    while await worker.run_once():
        pass

    async with session_factory() as session:
        completed = await session.get(InitialAnalysisRun, run.id)
        messages = await session.scalar(select(func.count(TelegramMessage.id)))
        signals = list(await session.scalars(select(Signal)))
        jobs = list(await session.scalars(select(BackgroundJob)))
        personal = await session.scalar(
            select(TelegramDialog).where(TelegramDialog.dialog_type == "personal")
        )
    assert completed.status == "completed" and completed.progress_percent == 100
    assert messages == 2
    assert {item.signal_type for item in signals} >= {"contract_question"}, [
        (item.job_type, item.status, item.last_error) for item in jobs
    ]
    assert personal.selected is True and personal.requires_user_confirmation is False
    assert gateway.fetch_calls == [(1001, 0), (1002, 0)]

    # Initial sync now enters the same Signal lifecycle as incremental ingestion.
    async with session_factory() as session:
        assert await session.scalar(select(func.count(TelegramMessage.id))) == 2
        signal_count = await session.scalar(select(func.count(Signal.id)))
        assert signal_count == len(signals)


@pytest.mark.asyncio
async def test_dialog_exclusion_is_tenant_scoped(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant, _, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    await service.refresh_catalog(tenant.id)
    async with session_factory() as session:
        dialog = await session.scalar(
            select(TelegramDialog).where(TelegramDialog.telegram_dialog_id == 1002)
        )
    with pytest.raises(LookupError):
        await service.exclude_dialog("00000000-0000-0000-0000-000000000000", dialog.id)
    await service.exclude_dialog(tenant.id, dialog.id)
    async with session_factory() as session:
        updated = await session.get(TelegramDialog, dialog.id)
        assert updated.excluded and not updated.selected


@pytest.mark.asyncio
async def test_reconnect_disconnect_and_irreversible_clear(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant, gateway, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    # Reusing the same phone and pending session must not violate secret uniqueness.
    await service.begin_login(tenant.id, "+79990001122")
    await service.complete_login(tenant.id, code="67890")
    await service.refresh_catalog(tenant.id)
    await service.select_scope(tenant.id, 10, personal_dialogs_consent=False)

    await service.disconnect(tenant.id)
    disconnected = await service.get(tenant.id)
    assert disconnected.status == "disconnected"
    assert disconnected.session_secret_id is None
    assert gateway.terminated_sessions == ["authorized-session"]

    await service.clear_data(tenant.id)
    assert await service.get(tenant.id) is None
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(EncryptedSecret.id)).where(
                    EncryptedSecret.tenant_id == tenant.id,
                    EncryptedSecret.kind.like("telegram_%"),
                )
            )
            == 0
        )
