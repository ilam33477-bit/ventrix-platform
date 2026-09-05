from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BotInstance, GroupIntegration, TenantMembership


def group_is_approved(group: GroupIntegration) -> bool:
    return bool(group.approved_at and group.approved_by_telegram_user_id and group.bot_instance_id)


async def observe_group(
    session: AsyncSession,
    *,
    tenant_id: str,
    bot_instance_id: str,
    chat_id: int,
    title: str,
    bot_status: str,
    participants_count: int | None = None,
    approver_user_id: int | None = None,
) -> GroupIntegration:
    if chat_id >= 0:
        raise ValueError("a Telegram group chat id is required")
    bot = await session.scalar(
        select(BotInstance.id).where(
            BotInstance.id == bot_instance_id,
            BotInstance.tenant_id == tenant_id,
            BotInstance.enabled.is_(True),
            BotInstance.is_active.is_(True),
            BotInstance.deleted_at.is_(None),
        )
    )
    if bot is None:
        raise PermissionError("active project bot required")
    if approver_user_id is not None:
        manager = await session.scalar(
            select(TenantMembership.id).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.telegram_user_id == approver_user_id,
                TenantMembership.status == "active",
                TenantMembership.role.in_(("owner", "manager")),
            )
        )
        if not manager or bot_status != "administrator":
            raise PermissionError("project manager and Telegram bot administration required")
    group = await session.scalar(
        select(GroupIntegration).where(
            GroupIntegration.tenant_id == tenant_id,
            GroupIntegration.telegram_chat_id == chat_id,
        )
    )
    if group is None:
        group = GroupIntegration(tenant_id=tenant_id, telegram_chat_id=chat_id, title=title)
        session.add(group)
    if group.bot_instance_id != bot_instance_id or bot_status != "administrator":
        group.approved_at = None
        group.approved_by_telegram_user_id = None
    group.bot_instance_id = bot_instance_id
    group.title = title or group.title
    if participants_count is not None:
        group.participants_count = participants_count
    group.last_verified_at = datetime.now(UTC)
    if approver_user_id is not None:
        group.approved_at = datetime.now(UTC)
        group.approved_by_telegram_user_id = approver_user_id
        group.status = "active"
    elif group.status != "disabled":
        group.status = (
            "active"
            if bot_status == "administrator" and group_is_approved(group)
            else "pending"
            if bot_status in {"administrator", "member", "restricted"}
            else "revoked"
        )
    await session.flush()
    return group
