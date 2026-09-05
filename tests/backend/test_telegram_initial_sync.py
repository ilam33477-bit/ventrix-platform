from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from services.backend.intelligence.signals import SignalService
from services.backend.jobs.queue import SQLiteJobQueue
from services.backend.jobs.worker import BackgroundWorker
from services.backend.models import (
    BackgroundJob,
    Employee,
    EncryptedSecret,
    InitialAnalysisRun,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantMembership,
)
from services.backend.services.employee_access import ConnectionEmployeeConflict
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


@pytest.mark.parametrize("require_2fa", [False, True])
async def test_shared_login_atomically_binds_employee_and_membership(
    session_factory, make_service, tenant_payload, encryption_key, require_2fa
):
    tenant, _, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key, require_2fa=require_2fa
    )
    connection = await service.get(tenant.id)
    async with session_factory() as session:
        employee = await session.get(Employee, connection.assigned_employee_id)
        assert employee.telegram_user_id == connection.telegram_user_id == 777001
        member = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant.id, TenantMembership.employee_id == employee.id
            )
        )
        assert member.telegram_user_id == employee.telegram_user_id
        assert member.role == "employee" and member.status == "active"


async def test_pending_2fa_does_not_grant_employee_access(
    session_factory, make_service, tenant_payload, encryption_key
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    service = TelegramConnectionService(
        session_factory, EncryptionService(encryption_key), FakeTelegramGateway(require_2fa=True)
    )
    pending = await service.begin_login(tenant.id, "+79990001122")
    result = await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    assert result.status == "awaiting_2fa" and result.assigned_employee_id is None
    assert result.session_secret_id is None
    async with session_factory() as session:
        assert (
            await session.scalar(select(Employee.id).where(Employee.tenant_id == tenant.id)) is None
        )


async def test_owner_login_preserves_ownership_in_shared_service(
    session_factory, make_service, tenant_payload, encryption_key, monkeypatch
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    gateway = FakeTelegramGateway()

    async def owner_login(*args, **kwargs):
        return LoginResult(
            "connected", "owner-session", tenant_payload.owner_telegram_user_id, "owner", "Owner"
        )

    monkeypatch.setattr(gateway, "complete_login", owner_login)
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    pending = await service.begin_login(tenant.id, "+79990001122")
    result = await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    async with session_factory() as session:
        employee = await session.get(Employee, result.assigned_employee_id)
        member = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant.id,
                TenantMembership.telegram_user_id == employee.telegram_user_id,
            )
        )
        assert member.role == "owner" and member.status == "active"


async def test_parallel_projects_do_not_share_employee_or_membership_bindings(
    session_factory, make_service, tenant_payload, encryption_key
):
    async with session_factory() as session:
        first = await make_service(session).create_tenant(tenant_payload)
        second = await make_service(session).create_tenant(
            tenant_payload.model_copy(update={"name": "Second project"})
        )
    service = TelegramConnectionService(
        session_factory, EncryptionService(encryption_key), FakeTelegramGateway()
    )

    async def connect(tenant):
        pending = await service.begin_login(tenant.id, "+79990001122")
        return await service.complete_login(tenant.id, connection_id=pending.id, code="12345")

    one, two = await asyncio.gather(connect(first), connect(second))
    assert one.id != two.id
    assert one.assigned_employee_id != two.assigned_employee_id
    assert one.session_secret_id != two.session_secret_id
    async with session_factory() as session:
        for tenant, connection in [(first, one), (second, two)]:
            employee = await session.get(Employee, connection.assigned_employee_id)
            assert employee.tenant_id == tenant.id
            member = await session.scalar(
                select(TenantMembership).where(TenantMembership.employee_id == employee.id)
            )
            assert member.tenant_id == tenant.id


@pytest.mark.parametrize("revoked", [False, True])
async def test_reconnect_reuses_employee_preserving_settings_and_access(
    session_factory, make_service, tenant_payload, encryption_key, revoked
):
    tenant, _, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    original = await service.get(tenant.id)
    async with session_factory() as session:
        employee = await session.get(Employee, original.assigned_employee_id)
        employee.criticality_threshold = 72
        employee.notifications_enabled = False
        employee.display_name = "Имя от администратора"
        member = await session.scalar(
            select(TenantMembership).where(TenantMembership.employee_id == employee.id)
        )
        member.status = "inactive" if revoked else "active"
        await session.commit()
    pending = await service.begin_login(tenant.id, "+79990001122")
    result = await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    assert result.id == original.id
    assert result.assigned_employee_id == original.assigned_employee_id
    async with session_factory() as session:
        assert await session.get(TelegramConnection, pending.id) is None
        employee = await session.get(Employee, result.assigned_employee_id)
        assert employee.criticality_threshold == 72
        assert employee.notifications_enabled is False
        assert employee.display_name == "Имя от администратора"
        assert (
            await session.scalar(
                select(func.count()).select_from(Employee).where(Employee.tenant_id == tenant.id)
            )
            == 1
        )
        updated = await session.get(TenantMembership, member.id)
        assert updated.status == ("inactive" if revoked else "active")


@pytest.mark.parametrize("selected_id", [None, 777001, 888002])
async def test_selected_employee_requires_matching_verified_telegram_identity(
    session_factory, make_service, tenant_payload, encryption_key, selected_id
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        employee = Employee(
            tenant_id=tenant.id, display_name="Выбранный сотрудник", telegram_user_id=selected_id
        )
        session.add(employee)
        await session.commit()
    gateway = FakeTelegramGateway()
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    pending = await service.begin_login(tenant.id, "+79990001122", employee.id)
    if selected_id == 888002:
        with pytest.raises(ConnectionEmployeeConflict):
            await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
        assert gateway.terminated_sessions == ["authorized-session"]
    else:
        result = await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
        assert result.assigned_employee_id == employee.id
    async with session_factory() as session:
        stored = await session.get(Employee, employee.id)
        connection = await session.get(TelegramConnection, pending.id)
        if selected_id == 888002:
            assert stored.telegram_user_id == 888002
            assert connection.session_secret_id is None
            assert connection.status == "disconnected"
            assert connection.deleted_at is not None
            assert (
                await session.scalar(
                    select(TenantMembership.id).where(TenantMembership.employee_id == employee.id)
                )
                is None
            )
        else:
            assert stored.telegram_user_id == 777001


async def test_reconnect_cannot_transfer_existing_account_to_another_employee(
    session_factory, make_service, tenant_payload, encryption_key
):
    tenant, gateway, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    original = await service.get(tenant.id)
    async with session_factory() as session:
        another = Employee(tenant_id=tenant.id, display_name="Другой сотрудник")
        session.add(another)
        await session.commit()
    pending = await service.begin_login(tenant.id, "+79990001122", another.id)
    with pytest.raises(ConnectionEmployeeConflict):
        await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    current = await service.get(tenant.id, original.id)
    assert current.status == "connected"
    assert current.assigned_employee_id == original.assigned_employee_id
    assert current.session_secret_id == original.session_secret_id
    assert gateway.terminated_sessions == []  # Fake gateway reused the existing session key.


@pytest.mark.parametrize("change", ["cancelled", "superseded", "challenge_changed", "reassigned"])
async def test_login_result_cannot_revive_cancelled_or_superseded_challenge(
    session_factory, make_service, tenant_payload, encryption_key, monkeypatch, change
):
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
    gateway = FakeTelegramGateway()
    service = TelegramConnectionService(session_factory, EncryptionService(encryption_key), gateway)
    pending = await service.begin_login(tenant.id, "+79990001122")
    complete = gateway.complete_login

    async def delayed_result(*args, **kwargs):
        result = await complete(*args, **kwargs)
        if change == "cancelled":
            await service.cancel_login(tenant.id, pending.id)
        elif change == "superseded":
            await service.begin_login(tenant.id, "+79990003344")
        else:
            async with session_factory() as session:
                stored = await session.get(TelegramConnection, pending.id)
                if change == "reassigned":
                    employee = Employee(tenant_id=tenant.id, display_name="Новый ответственный")
                    session.add(employee)
                    await session.flush()
                    stored.assigned_employee_id = employee.id
                else:
                    secret = service._secret(tenant.id, "telegram_phone_code_hash", "new-code-hash")
                    session.add(secret)
                    await session.flush()
                    stored.phone_code_hash_secret_id = secret.id
                await session.commit()
        return result

    monkeypatch.setattr(gateway, "complete_login", delayed_result)
    with pytest.raises(TelegramConnectionError):
        await service.complete_login(tenant.id, connection_id=pending.id, code="12345")
    async with session_factory() as session:
        stored = await session.get(TelegramConnection, pending.id)
        assert stored.session_secret_id is None
        if change == "reassigned":
            employee = await session.get(Employee, stored.assigned_employee_id)
            assert employee.telegram_user_id is None
        else:
            assert stored.assigned_employee_id is None
            assert (
                await session.scalar(select(Employee.id).where(Employee.tenant_id == tenant.id))
                is None
            )


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
async def test_default_scope_accepts_naive_sqlite_dialog_timestamps(
    session_factory, make_service, tenant_payload, encryption_key
) -> None:
    tenant, _, service = await connected_service(
        session_factory, make_service, tenant_payload, encryption_key
    )
    await service.refresh_catalog(tenant.id)
    async with session_factory() as session:
        dialog = await session.scalar(
            select(TelegramDialog).where(
                TelegramDialog.tenant_id == tenant.id,
                TelegramDialog.dialog_type == "personal",
            )
        )
        dialog.last_message_at = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
        await session.commit()

    connection = await service.activate_default_scope(tenant.id, history_days=14)

    async with session_factory() as session:
        dialog = await session.get(TelegramDialog, dialog.id)
    assert connection.progress_stage == "personal_sources_enabled"
    assert dialog.selected is True


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
