from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import errors, functions, utils

from ..jobs.queue import JobDeferred, JobLease
from ..models import (
    BackgroundJob,
    BotInstance,
    Employee,
    TelegramConnection,
    Tenant,
    TenantMembership,
)

ACTIVATION_JOB = "telegram.start_employee_bot"
ACTIVATION_PARAMETER = "employee_onboarding"


async def enqueue_employee_activation(
    session: AsyncSession, connection: TelegramConnection
) -> str | None:
    """Called inside the login transaction; no network calls and no independent commit."""
    member = await session.scalar(
        select(TenantMembership).where(
            TenantMembership.tenant_id == connection.tenant_id,
            TenantMembership.employee_id == connection.assigned_employee_id,
            TenantMembership.telegram_user_id == connection.telegram_user_id,
            TenantMembership.role == "employee",
            TenantMembership.status == "active",
        )
    )
    if member is None or member.bot_started_at is not None:
        return None
    bots = list(
        await session.scalars(
            select(BotInstance)
            .where(
                BotInstance.tenant_id == connection.tenant_id,
                BotInstance.enabled.is_(True),
                BotInstance.is_active.is_(True),
                BotInstance.deleted_at.is_(None),
                BotInstance.verification_status == "verified",
            )
            .limit(2)
        )
    )
    if len(bots) != 1:
        return None  # Never guess a destination when project bot setup is incomplete.
    bot = bots[0]
    key = f"employee-bot-start:{connection.tenant_id}:{bot.id}:{member.telegram_user_id}"
    existing = await session.scalar(
        select(BackgroundJob.id).where(BackgroundJob.idempotency_key == key)
    )
    if existing:
        return existing
    job = BackgroundJob(
        tenant_id=connection.tenant_id,
        telegram_account_id=connection.id,
        job_type=ACTIVATION_JOB,
        category="telegram_rpc",
        cost_class="light",
        idempotency_key=key,
        scheduled_at=datetime.now(UTC),
        priority=5,
        max_attempts=8,
        payload_json={
            "employee_id": member.employee_id,
            "telegram_user_id": member.telegram_user_id,
            "bot_instance_id": bot.id,
            "telegram_bot_id": bot.telegram_bot_id,
            "random_id": secrets.randbits(63) or 1,
        },
    )
    session.add(job)
    await session.flush()
    return job.id


async def activation_target(
    session: AsyncSession, connection_id: str, job: JobLease
) -> BotInstance | None:
    """Recheck current identity and permissions, not just the queued snapshot."""
    if job.telegram_account_id != connection_id:
        return None
    connection = await session.get(TelegramConnection, connection_id)
    if (
        connection is None
        or connection.tenant_id != job.tenant_id
        or connection.deleted_at is not None
        or connection.status not in {"connected", "syncing", "ready"}
        or connection.session_secret_id is None
        or connection.assigned_employee_id != job.payload["employee_id"]
        or connection.telegram_user_id != job.payload["telegram_user_id"]
    ):
        return None
    tenant = await session.get(Tenant, job.tenant_id)
    if tenant is None or tenant.deleted_at is not None or tenant.status != "active":
        return None
    employee = await session.get(Employee, connection.assigned_employee_id)
    if (
        employee is None
        or employee.tenant_id != job.tenant_id
        or employee.status != "active"
        or employee.telegram_user_id != connection.telegram_user_id
    ):
        return None
    member = await session.scalar(
        select(TenantMembership).where(
            TenantMembership.tenant_id == job.tenant_id,
            TenantMembership.employee_id == employee.id,
            TenantMembership.telegram_user_id == connection.telegram_user_id,
            TenantMembership.role == "employee",
            TenantMembership.status == "active",
        )
    )
    if member is None or member.bot_started_at is not None:
        return None
    return await session.scalar(
        select(BotInstance).where(
            BotInstance.id == job.payload["bot_instance_id"],
            BotInstance.tenant_id == job.tenant_id,
            BotInstance.telegram_bot_id == job.payload["telegram_bot_id"],
            BotInstance.deleted_at.is_(None),
            BotInstance.enabled.is_(True),
            BotInstance.is_active.is_(True),
            BotInstance.verification_status == "verified",
        )
    )


async def start_employee_bot(actor: Any, job: JobLease) -> dict[str, Any]:
    async with actor.rpc_lock:
        async with actor.transactions.session_factory() as session:
            bot = await activation_target(session, actor.connection.id, job)
            stored = await session.get(BackgroundJob, job.id)
            if stored is not None and stored.status == "completed":
                return stored.result_json or {"status": "already_processed"}
        if bot is None:
            return {"status": "skipped", "reason": "access_changed_or_bot_already_started"}
        if not actor.client.is_connected() or bot.runtime_status != "running":
            raise JobDeferred(30, "employee_bot_start_waiting_for_runtime")
        try:
            me = await actor.client.get_me()
            if me is None or me.id != job.payload["telegram_user_id"]:
                return {"status": "requires_manual_start", "reason": "session_identity_mismatch"}
            entity = await actor.client.get_entity(bot.username)
            if not getattr(entity, "bot", False) or entity.id != bot.telegram_bot_id:
                return {"status": "requires_manual_start", "reason": "bot_identity_mismatch"}
            # Network resolution can take time: do not use stale access after it.
            async with actor.transactions.session_factory() as session:
                if await activation_target(session, actor.connection.id, job) is None:
                    return {"status": "skipped", "reason": "access_changed"}
            await actor.client(
                functions.messages.StartBotRequest(
                    bot=utils.get_input_user(entity),
                    peer=utils.get_input_peer(entity),
                    random_id=int(job.payload["random_id"]),
                    start_param=ACTIVATION_PARAMETER,
                )
            )
        except errors.FloodWaitError as exc:
            raise JobDeferred(max(1, exc.seconds), "employee_bot_start_flood_wait") from None
        except (
            errors.UserIsBlockedError,
            errors.YouBlockedUserError,
            errors.ChatWriteForbiddenError,
        ):
            # Respect a block. Do not unblock the bot or repeatedly send /start.
            return {"status": "requires_manual_start", "reason": "bot_blocked"}
        except errors.RandomIdDuplicateError:
            return {"status": "start_requested", "deduplicated": True}
        return {"status": "start_requested"}
