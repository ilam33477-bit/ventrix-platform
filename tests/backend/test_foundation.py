from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from services.backend.bot.auth import is_platform_owner
from services.backend.models import AuditLog, BotInstance, EncryptedSecret
from services.backend.schemas import AIProfileUpdate, BotCreate, TenantCreate
from services.backend.services.encryption import EncryptionError, EncryptionService
from services.backend.services.foundation import TenantNotFoundError


def test_encryption_round_trip_and_randomized_ciphertext(encryption_key: str) -> None:
    service = EncryptionService(encryption_key)
    token = "mock-telegram-token-must-remain-secret"
    first = service.encrypt(token)
    second = service.encrypt(token)
    assert first != second
    assert token.encode() not in first
    assert service.decrypt(first) == token
    assert service.fingerprint(token) == service.fingerprint(token)


def test_wrong_key_cannot_decrypt(encryption_key: str) -> None:
    ciphertext = EncryptionService(encryption_key).encrypt("secret")
    from cryptography.fernet import Fernet

    with pytest.raises(EncryptionError):
        EncryptionService(Fernet.generate_key().decode()).decrypt(ciphertext)


def test_owner_telegram_id_guard() -> None:
    assert is_platform_owner(100200300, 100200300)
    assert not is_platform_owner(100200301, 100200300)
    assert not is_platform_owner(None, 100200300)


def test_bot_token_schema_masks_secret() -> None:
    token = "mock-telegram-token-must-remain-secret"
    payload = BotCreate(token=token)
    assert token not in repr(payload)
    assert payload.token.get_secret_value() == token


@pytest.mark.asyncio
async def test_create_tenant_profile_and_audit(
    session_factory, make_service, tenant_payload: TenantCreate
) -> None:
    async with session_factory() as session:
        tenant = await make_service(session).create_tenant(tenant_payload)
        assert tenant.settings.timezone == "Europe/Moscow"
        assert tenant.ai_profile.products == ["Implementation", "Support"]
        assert tenant.ai_profile.additional_instructions.startswith("Use the term")
        logs = list(await session.scalars(select(AuditLog)))
        assert [log.action for log in logs] == ["tenant.created"]
        assert logs[0].tenant_id == tenant.id


@pytest.mark.asyncio
async def test_update_ai_profile_is_versioned(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        service = make_service(session)
        tenant = await service.create_tenant(tenant_payload)
        profile = await service.update_ai_profile(
            tenant.id,
            AIProfileUpdate(
                typical_processes=["sales", "support"],
                sales_stages=["lead", "qualified", "won"],
                typical_promises=["send proposal"],
                typical_objections=["too expensive"],
                critical_events=["refund request"],
                significant_amounts=[500000],
                prohibited_conclusions=["automatic dismissal recommendation"],
            ),
        )
        assert profile.version == 2
        assert profile.significant_amounts == [500000]


@pytest.mark.asyncio
async def test_bot_token_is_verified_encrypted_scoped_and_audited(
    session_factory, make_service, tenant_payload, verifier
) -> None:
    submitted = "mock-telegram-token-must-remain-secret"
    async with session_factory() as session:
        service = make_service(session)
        tenant = await service.create_tenant(tenant_payload)
        second_payload = tenant_payload.model_copy(
            update={"name": "Second Tenant", "owner_telegram_user_id": 555000222}
        )
        second = await service.create_tenant(second_payload)
        bot = await service.create_bot(tenant.id, BotCreate(token=submitted))
        assert verifier.tokens == [submitted]
        assert bot.tenant_id == tenant.id
        assert bot.username == "axiom_ops_bot"
        assert await service.list_bots(second.id) == []

        secret = await session.scalar(
            select(EncryptedSecret).where(EncryptedSecret.id == bot.secret_id)
        )
        assert secret is not None
        assert submitted.encode() not in secret.ciphertext
        assert service.encryption.decrypt(secret.ciphertext) == submitted
        serialized_logs = " ".join(
            str(log.details) for log in await session.scalars(select(AuditLog))
        )
        assert submitted not in serialized_logs
        assert await session.scalar(select(BotInstance).where(BotInstance.id == bot.id)) is not None

        with pytest.raises(TenantNotFoundError):
            await service.list_bots(uuid4())


@pytest.mark.asyncio
async def test_owner_bot_runtime_controls_rotation_and_soft_delete(
    session_factory, make_service, tenant_payload
) -> None:
    async with session_factory() as session:
        service = make_service(session)
        tenant = await service.create_tenant(tenant_payload)
        bot = await service.create_bot(
            tenant.id, BotCreate(token="mock-original-token-long-enough")
        )
        original_secret_id = bot.secret_id
        generation = bot.runtime_generation

        stopped = await service.set_bot_enabled(bot.id, False)
        assert not stopped.enabled and stopped.runtime_generation == generation + 1
        started = await service.set_bot_enabled(bot.id, True)
        restarted = await service.restart_bot(bot.id)
        assert started.enabled and restarted.runtime_generation == generation + 3

        rotated = await service.rotate_bot_token(bot.id, "mock-rotated-token-long-enough")
        assert rotated.secret_id != original_secret_id
        assert rotated.runtime_generation == generation + 4
        old_secret = await session.get(EncryptedSecret, original_secret_id)
        assert old_secret.deleted_at is not None

        await service.delete_bot(bot.id)
        deleted = await session.get(BotInstance, bot.id)
        assert deleted.deleted_at is not None and not deleted.enabled and not deleted.is_active
