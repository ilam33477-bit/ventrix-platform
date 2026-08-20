from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    AuditLog,
    BotInstance,
    EncryptedSecret,
    PlatformOwner,
    Tenant,
    TenantAIProfile,
    TenantMembership,
    TenantSettings,
)
from ..repositories.tenants import TenantRepository
from ..schemas import AIProfileUpdate, BotCreate, TenantCreate, TenantUpdate
from .encryption import EncryptionService
from .telegram import VerifiedBot


class TenantNotFoundError(LookupError):
    pass


class BotAlreadyExistsError(RuntimeError):
    pass


class BotNotFoundError(LookupError):
    pass


class BotVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedBot: ...


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    blocked = ("token", "secret", "password", "ciphertext")
    return {
        key: value
        for key, value in details.items()
        if not any(word in key.lower() for word in blocked)
    }


class FoundationService:
    def __init__(
        self,
        session: AsyncSession,
        owner_telegram_id: int,
        encryption: EncryptionService,
        verifier: BotVerifier,
        source: str = "api",
        owner_username: str | None = None,
    ) -> None:
        self.session = session
        self.owner_telegram_id = owner_telegram_id
        self.encryption = encryption
        self.verifier = verifier
        self.source = source
        self.owner_username = owner_username
        self.tenants = TenantRepository(session)

    async def ensure_owner(self, username: str | None = None) -> PlatformOwner:
        username = username or self.owner_username
        owner = await self.session.scalar(
            select(PlatformOwner).where(PlatformOwner.telegram_user_id == self.owner_telegram_id)
        )
        if owner is None:
            owner = PlatformOwner(
                telegram_user_id=self.owner_telegram_id,
                telegram_username=username,
                is_active=True,
            )
            self.session.add(owner)
            await self.session.flush()
        elif username and owner.telegram_username != username:
            owner.telegram_username = username
        return owner

    async def create_tenant(self, payload: TenantCreate, *, commit: bool = True) -> Tenant:
        owner = await self.ensure_owner()
        tenant = Tenant(
            name=payload.name.strip(),
            owner_name=payload.owner_name.strip(),
            owner_telegram_username=payload.owner_telegram_username,
            owner_telegram_user_id=payload.owner_telegram_user_id,
            niche=payload.niche.strip(),
            business_description=payload.business_description.strip(),
            products_services=payload.products_services.strip(),
            target_audience=payload.target_audience.strip(),
            plan=payload.plan.strip(),
            subscription_expires_at=payload.subscription_expires_at,
            status="active",
        )
        tenant.settings = TenantSettings(
            working_hours=payload.working_hours,
            timezone=payload.timezone,
            response_sla_minutes=payload.response_sla_minutes,
            critical_problem_criteria=payload.critical_problem_criteria.strip(),
            daily_report_time=payload.daily_report_time,
            active_dialog_days=payload.active_dialog_days,
            message_history_days=payload.message_history_days,
        )
        tenant.ai_profile = TenantAIProfile(
            niche=payload.niche.strip(),
            business_description=payload.business_description.strip(),
            products=[
                item.strip() for item in payload.products_services.split(",") if item.strip()
            ],
            target_audience=payload.target_audience.strip(),
            typical_processes=[],
            sales_stages=[],
            typical_promises=[],
            typical_objections=[],
            critical_events=[payload.critical_problem_criteria.strip()],
            significant_amounts=[],
            response_sla_minutes=payload.response_sla_minutes,
            prohibited_conclusions=[],
            additional_instructions=payload.additional_ai_instructions.strip(),
        )
        self.session.add(tenant)
        await self.session.flush()
        self.session.add(
            TenantMembership(
                tenant_id=tenant.id,
                telegram_user_id=tenant.owner_telegram_user_id,
                role="owner",
                status="active",
            )
        )
        await self._audit(
            owner, "tenant.created", "tenant", tenant.id, tenant.id, {"name": tenant.name}
        )
        if commit:
            await self.session.commit()
            return await self._require_tenant(tenant.id)
        await self.session.flush()
        return tenant

    async def update_tenant(self, tenant_id: UUID | str, payload: TenantUpdate) -> Tenant:
        tenant = await self._require_tenant(tenant_id)
        owner = await self.ensure_owner()
        values = payload.model_dump(exclude_unset=True)
        setting_fields = {
            "working_hours",
            "timezone",
            "response_sla_minutes",
            "critical_problem_criteria",
            "daily_report_time",
        }
        for key, value in values.items():
            target = tenant.settings if key in setting_fields else tenant
            setattr(target, key, value)
        if "response_sla_minutes" in values:
            tenant.ai_profile.response_sla_minutes = values["response_sla_minutes"]
        await self._audit(
            owner, "tenant.updated", "tenant", tenant.id, tenant.id, {"fields": sorted(values)}
        )
        await self.session.commit()
        return await self._require_tenant(tenant_id)

    async def update_ai_profile(
        self, tenant_id: UUID | str, payload: AIProfileUpdate
    ) -> TenantAIProfile:
        tenant = await self._require_tenant(tenant_id)
        owner = await self.ensure_owner()
        values = payload.model_dump(exclude_unset=True)
        if "response_sla_minutes" in values:
            tenant.settings.response_sla_minutes = values["response_sla_minutes"]
        for key, value in values.items():
            setattr(tenant.ai_profile, key, value)
        tenant.ai_profile.version += 1
        await self._audit(
            owner,
            "tenant.ai_profile.updated",
            "tenant_ai_profile",
            tenant.ai_profile.id,
            tenant.id,
            {"fields": sorted(values), "version": tenant.ai_profile.version},
        )
        await self.session.commit()
        await self.session.refresh(tenant.ai_profile)
        return tenant.ai_profile

    async def create_bot(self, tenant_id: UUID | str, payload: BotCreate) -> BotInstance:
        tenant = await self._require_tenant(tenant_id)
        owner = await self.ensure_owner()
        token = payload.token.get_secret_value()
        verified = await self.verifier.verify(token)
        existing = await self.session.scalar(
            select(BotInstance).where(
                (BotInstance.telegram_bot_id == verified.bot_id)
                | (BotInstance.username == verified.username)
            )
        )
        if existing is not None:
            raise BotAlreadyExistsError("This Telegram bot is already connected")
        secret = EncryptedSecret(
            tenant_id=tenant.id,
            kind="telegram_bot_token",
            ciphertext=self.encryption.encrypt(token),
            fingerprint=self.encryption.fingerprint(token),
            key_version=1,
        )
        self.session.add(secret)
        await self.session.flush()
        bot = BotInstance(
            tenant_id=tenant.id,
            secret_id=secret.id,
            telegram_bot_id=verified.bot_id,
            username=verified.username,
            display_name=verified.display_name,
            verification_status="verified",
            verified_at=datetime.now(UTC),
            is_active=True,
        )
        self.session.add(bot)
        await self.session.flush()
        await self._audit(
            owner,
            "tenant.bot.created",
            "bot_instance",
            bot.id,
            tenant.id,
            {
                "telegram_bot_id": verified.bot_id,
                "username": verified.username,
                "status": "verified",
            },
        )
        await self.session.commit()
        return bot

    async def list_tenants(self) -> list[Tenant]:
        return await self.tenants.list()

    async def get_tenant(self, tenant_id: UUID | str) -> Tenant:
        return await self._require_tenant(tenant_id)

    async def set_tenant_access(
        self,
        tenant_id: UUID | str,
        *,
        expires_at: date | None = None,
        extend_days: int | None = None,
        active: bool | None = None,
    ) -> Tenant:
        tenant = await self._require_tenant(tenant_id)
        owner = await self.ensure_owner()
        if extend_days is not None:
            if extend_days <= 0:
                raise ValueError("extend_days must be positive")
            today = datetime.now(UTC).date()
            base = max(tenant.subscription_expires_at or today, today)
            tenant.subscription_expires_at = base + timedelta(days=extend_days)
        elif expires_at is not None:
            tenant.subscription_expires_at = expires_at
        if active is not None:
            tenant.status = "active" if active else "suspended"
        await self._audit(
            owner,
            "tenant.access.updated",
            "tenant",
            tenant.id,
            tenant.id,
            {
                "expires_at": tenant.subscription_expires_at.isoformat()
                if tenant.subscription_expires_at
                else None,
                "status": tenant.status,
            },
        )
        await self.session.commit()
        return await self._require_tenant(tenant.id)

    async def delete_tenant(self, tenant_id: UUID | str) -> None:
        tenant = await self._require_tenant(tenant_id)
        owner = await self.ensure_owner()
        now = datetime.now(UTC)
        for bot in tenant.bots:
            bot.enabled = False
            bot.is_active = False
            bot.runtime_status = "stopping"
            bot.deleted_at = now
            bot.secret.deleted_at = now
            bot.runtime_generation += 1
        tenant.status = "deleted"
        tenant.deleted_at = now
        await self._audit(
            owner,
            "tenant.deleted",
            "tenant",
            tenant.id,
            tenant.id,
            {"name": tenant.name},
        )
        await self.session.commit()

    async def list_bots(self, tenant_id: UUID | str) -> list[BotInstance]:
        await self._require_tenant(tenant_id)
        return await self.tenants.list_bots(tenant_id)

    async def get_bot(self, bot_id: UUID | str) -> BotInstance:
        bot = await self.session.scalar(
            select(BotInstance).where(
                BotInstance.id == str(bot_id), BotInstance.deleted_at.is_(None)
            )
        )
        if bot is None:
            raise BotNotFoundError(str(bot_id))
        return bot

    async def set_bot_enabled(self, bot_id: UUID | str, enabled: bool) -> BotInstance:
        bot = await self.get_bot(bot_id)
        owner = await self.ensure_owner()
        bot.enabled = enabled
        bot.runtime_status = "starting" if enabled else "stopping"
        bot.runtime_generation += 1
        await self._audit(
            owner,
            "tenant.bot.started" if enabled else "tenant.bot.stopped",
            "bot_instance",
            bot.id,
            bot.tenant_id,
            {"enabled": enabled, "generation": bot.runtime_generation},
        )
        await self.session.commit()
        return bot

    async def restart_bot(self, bot_id: UUID | str) -> BotInstance:
        bot = await self.get_bot(bot_id)
        owner = await self.ensure_owner()
        bot.enabled = True
        bot.runtime_status = "starting"
        bot.runtime_generation += 1
        await self._audit(
            owner,
            "tenant.bot.restart_requested",
            "bot_instance",
            bot.id,
            bot.tenant_id,
            {"generation": bot.runtime_generation},
        )
        await self.session.commit()
        return bot

    async def verify_bot(self, bot_id: UUID | str) -> VerifiedBot:
        bot = await self.get_bot(bot_id)
        token = self.encryption.decrypt(bot.secret.ciphertext)
        try:
            verified = await self.verifier.verify(token)
        finally:
            token = ""
        if verified.bot_id != bot.telegram_bot_id:
            raise RuntimeError("Telegram token belongs to another bot")
        bot.verification_status = "verified"
        bot.verified_at = datetime.now(UTC)
        bot.last_error = None
        await self.session.commit()
        return verified

    async def rotate_bot_token(self, bot_id: UUID | str, token: str) -> BotInstance:
        bot = await self.get_bot(bot_id)
        owner = await self.ensure_owner()
        try:
            verified = await self.verifier.verify(token)
            if verified.bot_id != bot.telegram_bot_id:
                raise RuntimeError("New token belongs to another Telegram bot")
            old_secret = bot.secret
            secret = EncryptedSecret(
                tenant_id=bot.tenant_id,
                kind="telegram_bot_token",
                ciphertext=self.encryption.encrypt(token),
                fingerprint=self.encryption.fingerprint(token),
                key_version=1,
            )
            self.session.add(secret)
            await self.session.flush()
            bot.secret_id = secret.id
            bot.secret = secret
            bot.username = verified.username
            bot.display_name = verified.display_name
            bot.verified_at = datetime.now(UTC)
            bot.verification_status = "verified"
            bot.enabled = True
            bot.runtime_status = "starting"
            bot.runtime_generation += 1
            old_secret.deleted_at = datetime.now(UTC)
            await self._audit(
                owner,
                "tenant.bot.token_rotated",
                "bot_instance",
                bot.id,
                bot.tenant_id,
                {"telegram_bot_id": bot.telegram_bot_id, "generation": bot.runtime_generation},
            )
            await self.session.commit()
            return bot
        finally:
            token = ""

    async def delete_bot(self, bot_id: UUID | str) -> None:
        bot = await self.get_bot(bot_id)
        owner = await self.ensure_owner()
        now = datetime.now(UTC)
        bot.enabled = False
        bot.is_active = False
        bot.runtime_status = "stopping"
        bot.deleted_at = now
        bot.secret.deleted_at = now
        bot.runtime_generation += 1
        await self._audit(
            owner,
            "tenant.bot.deleted",
            "bot_instance",
            bot.id,
            bot.tenant_id,
            {"telegram_bot_id": bot.telegram_bot_id},
        )
        await self.session.commit()

    async def _require_tenant(self, tenant_id: UUID | str) -> Tenant:
        tenant = await self.tenants.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(str(tenant_id))
        return tenant

    async def _audit(
        self,
        owner: PlatformOwner,
        action: str,
        entity_type: str,
        entity_id: UUID | str,
        tenant_id: UUID | str | None,
        details: dict[str, Any],
    ) -> None:
        self.session.add(
            AuditLog(
                actor_owner_id=owner.id,
                tenant_id=str(tenant_id) if tenant_id is not None else None,
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id),
                details=_safe_details(details),
                source=self.source,
            )
        )
