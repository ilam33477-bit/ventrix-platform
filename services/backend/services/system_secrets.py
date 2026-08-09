from __future__ import annotations

from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import EncryptedSecret
from .encryption import EncryptionService

SYSTEM_SECRET_KINDS = {
    "telegram_api_id": "platform_telegram_api_id",
    "telegram_api_hash": "platform_telegram_api_hash",
    "deepseek_api_key": "platform_deepseek_api_key",
}


def mask_secret(value: str | None) -> str:
    if not value:
        return "не настроено"
    if len(value) <= 8:
        return f"{value[:2]}••••{value[-2:]}"
    return f"{value[:4]}••••••{value[-4:]}"


class SystemSecretService:
    """Encrypted owner-only runtime overrides.

    Long-lived workers intentionally read these values on restart. This avoids
    mutating active Telegram or AI clients halfway through an operation.
    """

    def __init__(self, session: AsyncSession, encryption: EncryptionService) -> None:
        self.session = session
        self.encryption = encryption

    async def get(self, name: str) -> str | None:
        kind = SYSTEM_SECRET_KINDS[name]
        row = await self.session.scalar(
            select(EncryptedSecret)
            .where(
                EncryptedSecret.tenant_id.is_(None),
                EncryptedSecret.kind == kind,
                EncryptedSecret.deleted_at.is_(None),
            )
            .order_by(EncryptedSecret.created_at.desc())
            .limit(1)
        )
        return self.encryption.decrypt(row.ciphertext) if row else None

    async def set(self, name: str, value: str) -> None:
        normalized = self.validate(name, value)
        kind = SYSTEM_SECRET_KINDS[name]
        rows = list(
            await self.session.scalars(
                select(EncryptedSecret).where(
                    EncryptedSecret.tenant_id.is_(None),
                    EncryptedSecret.kind == kind,
                    EncryptedSecret.deleted_at.is_(None),
                )
            )
        )
        now = datetime.now(UTC)
        for row in rows:
            row.deleted_at = now
        self.session.add(
            EncryptedSecret(
                tenant_id=None,
                kind=kind,
                ciphertext=self.encryption.encrypt(normalized),
                fingerprint=self.encryption.fingerprint(normalized),
            )
        )
        await self.session.commit()

    async def stage(self, name: str, value: str) -> EncryptedSecret:
        normalized = self.validate(name, value)
        row = EncryptedSecret(
            tenant_id=None,
            kind=f"pending_{SYSTEM_SECRET_KINDS[name]}",
            ciphertext=self.encryption.encrypt(normalized),
            fingerprint=self.encryption.fingerprint(normalized),
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def confirm(self, name: str, staged_id: str) -> None:
        row = await self.session.scalar(
            select(EncryptedSecret).where(
                EncryptedSecret.id == staged_id,
                EncryptedSecret.tenant_id.is_(None),
                EncryptedSecret.kind == f"pending_{SYSTEM_SECRET_KINDS[name]}",
                EncryptedSecret.deleted_at.is_(None),
            )
        )
        if row is None:
            raise LookupError("Подготовленное значение не найдено")
        value = self.encryption.decrypt(row.ciphertext)
        await self.set(name, value)
        row.deleted_at = datetime.now(UTC)
        await self.session.commit()

    @staticmethod
    def validate(name: str, value: str) -> str:
        value = value.strip()
        if name == "telegram_api_id":
            if not value.isdigit() or int(value) <= 0:
                raise ValueError("API ID должен быть положительным числом")
        elif name == "telegram_api_hash":
            if len(value) < 24 or len(value) > 128:
                raise ValueError("API Hash имеет неверную длину")
        elif name == "deepseek_api_key":
            if len(value) < 16 or len(value) > 512:
                raise ValueError("DeepSeek key имеет неверную длину")
        else:
            raise ValueError("Неизвестный системный секрет")
        return value


async def load_runtime_secret_overrides(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Settings:
    """Resolve encrypted owner overrides once when a long-lived process starts."""

    encryption = EncryptionService(settings.app_encryption_key.get_secret_value())
    async with session_factory() as session:
        service = SystemSecretService(session, encryption)
        api_id = await service.get("telegram_api_id")
        api_hash = await service.get("telegram_api_hash")
        deepseek_key = await service.get("deepseek_api_key")
    updates: dict[str, object] = {}
    if api_id:
        updates["telegram_api_id"] = int(api_id)
    if api_hash:
        updates["telegram_api_hash"] = SecretStr(api_hash)
    if deepseek_key:
        updates["deepseek_api_key"] = SecretStr(deepseek_key)
    return settings.model_copy(update=updates)
