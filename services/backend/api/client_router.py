from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock_time
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.problems import ProblemStatus
from services.api.deepseek import DeepSeekProvider

from ..config import Settings, get_settings
from ..database import get_session
from ..intelligence.problem_lifecycle import (
    ACTIVE_PROBLEM_STATUSES,
    ProblemLifecycleService,
    TransitionRequest,
)
from ..jobs.queue import SQLiteJobQueue
from ..models import (
    AIUsageCall,
    BackgroundJob,
    BotInstance,
    Commitment,
    Employee,
    EncryptedSecret,
    GroupIntegration,
    InitialAnalysisRun,
    MonitoredSource,
    OperationalProblem,
    Permission,
    ProblemTransition,
    ProblemVerification,
    ProductEvent,
    Report,
    ReportMetric,
    ReportProblem,
    ReportSection,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    Tenant,
    TenantMembership,
    TenantSettings,
)
from ..repositories.client_data import TenantClientRepository
from ..scheduler.service import TenantAnalysisScheduler, next_analysis_time
from ..services.employee_access import claim_employee_by_username, sync_employee_membership
from ..services.encryption import EncryptionService
from ..services.onboarding_welcome import ensure_onboarding_welcome, fallback_welcome
from ..services.system_secrets import load_runtime_secret_overrides
from ..telegram_sessions.gateway import (
    TelegramFloodWait,
    TelegramLoginRestarted,
    TelegramSessionRevoked,
    TelethonGateway,
)
from ..telegram_sessions.service import (
    TelegramConnectionError,
    TelegramConnectionService,
    normalize_phone_number,
)
from ..timezones import normalize_timezone

router = APIRouter(prefix="/api/v1/client", tags=["tenant-client"])
logger = logging.getLogger(__name__)
VISIBLE_CONNECTION_STATUSES = frozenset(
    {"awaiting_code", "awaiting_2fa", "connected", "syncing", "ready", "reauthorization_required"}
)


class ProblemPatch(BaseModel):
    status: ProblemStatus
    reason: str = Field(min_length=1, max_length=2000)
    evidence: str | None = Field(default=None, max_length=4000)
    responsible_employee_id: str | None = None
    deadline_at: datetime | None = None


class ClientSettingsPatch(BaseModel):
    daily_report_time: clock_time | None = None
    timezone: str | None = None
    analysis_enabled: bool | None = None
    analysis_advance_minutes: int | None = Field(default=None)
    enabled_days: list[int] | None = None
    history_window_days: int | None = None
    signal_report_threshold: int | None = Field(default=None, ge=0, le=100)
    signal_problem_threshold: int | None = Field(default=None, ge=0, le=100)
    signal_immediate_threshold: int | None = Field(default=None, ge=0, le=100)
    manager_notification_threshold: int | None = Field(default=None, ge=0, le=100)
    employee_notification_threshold: int | None = Field(default=None, ge=0, le=100)
    group_notification_threshold: int | None = Field(default=None, ge=0, le=100)
    notification_immediate_threshold: int | None = Field(default=None, ge=0, le=100)
    critical_fast_lane_rules: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    ai_daily_soft_limit: int | None = Field(default=None, ge=1)
    ai_daily_hard_limit: int | None = Field(default=None, ge=1)
    employee_notifications_enabled: bool | None = None
    group_reminders_enabled: bool | None = None

    @field_validator("timezone")
    @classmethod
    def normalize_settings_timezone(cls, value: str | None) -> str | None:
        return normalize_timezone(value) if value is not None else None

    @field_validator("critical_fast_lane_rules")
    @classmethod
    def validate_fast_lane_rules(
        cls, value: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        normalized: list[dict[str, Any]] = []
        for index, rule in enumerate(value):
            any_terms = [str(item).strip().lower() for item in rule.get("contains_any", [])]
            all_terms = [str(item).strip().lower() for item in rule.get("contains_all", [])]
            any_terms = [item for item in any_terms if len(item) >= 3]
            all_terms = [item for item in all_terms if len(item) >= 3]
            has_metadata_match = any(
                rule.get(key) for key in ("attachment_name_any", "mime_types", "extensions")
            ) or bool(rule.get("requires_amount"))
            if not any_terms and not all_terms and not has_metadata_match:
                raise ValueError("fast lane rule requires contains_any or contains_all")
            criticality = int(rule.get("criticality", 95))
            if not 0 <= criticality <= 100:
                raise ValueError("fast lane criticality must be between 0 and 100")
            normalized.append(
                {
                    "id": str(rule.get("id") or f"rule-{index + 1}")[:64],
                    "enabled": bool(rule.get("enabled", True)),
                    "contains_any": any_terms[:20],
                    "contains_all": all_terms[:20],
                    "signal_types": [str(item)[:64] for item in rule.get("signal_types", [])][:20],
                    "attachment_name_any": [
                        str(item).strip().lower()[:100]
                        for item in rule.get("attachment_name_any", [])
                        if str(item).strip()
                    ][:20],
                    "mime_types": [
                        str(item).strip().lower()[:100]
                        for item in rule.get("mime_types", [])
                        if str(item).strip()
                    ][:20],
                    "extensions": [
                        str(item).strip().lower().lstrip(".")[:16]
                        for item in rule.get("extensions", [])
                        if str(item).strip()
                    ][:20],
                    "directions": [
                        item
                        for item in rule.get("directions", [])
                        if item in {"incoming", "outgoing"}
                    ],
                    "source_types": [
                        item
                        for item in rule.get("source_types", [])
                        if item in {"personal", "group", "channel"}
                    ],
                    "sender_roles": [
                        item
                        for item in rule.get("sender_roles", [])
                        if item in {"account_owner", "employee", "customer", "external", "unknown"}
                    ],
                    "requires_amount": bool(rule.get("requires_amount", False)),
                    "criticality": criticality,
                }
            )
        return normalized


class EmployeeCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    telegram_user_id: int | None = Field(default=None, gt=0)
    telegram_username: str | None = Field(default=None, max_length=64)
    role: Literal["manager", "employee", "observer"] = "employee"
    notifications_enabled: bool = True
    criticality_threshold: int = Field(default=85, ge=0, le=100)


class EmployeePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    telegram_user_id: int | None = Field(default=None, gt=0)
    telegram_username: str | None = Field(default=None, max_length=64)
    role: Literal["manager", "employee", "observer"] | None = None
    status: Literal["active", "inactive"] | None = None
    notifications_enabled: bool | None = None
    criticality_threshold: int | None = Field(default=None, ge=0, le=100)


class GroupIntegrationCreate(BaseModel):
    telegram_chat_id: int
    title: str = Field(min_length=1, max_length=300)
    notifications_enabled: bool = True
    minimum_criticality: int = Field(default=85, ge=0, le=100)
    reminder_cooldown_minutes: int = Field(default=120, ge=1, le=10_080)


class GroupIntegrationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: Literal["pending", "active", "disabled"] | None = None
    notifications_enabled: bool | None = None
    minimum_criticality: int | None = Field(default=None, ge=0, le=100)
    reminder_cooldown_minutes: int | None = Field(default=None, ge=1, le=10_080)


class CommitmentPatch(BaseModel):
    status: Literal["completed", "cancelled"]
    reason: str = Field(min_length=1, max_length=2000)


class TelegramLoginStart(BaseModel):
    phone: str = Field(min_length=8, max_length=24)
    employee_id: str | None = None
    create_employee: bool = False

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_number(value)


class TelegramLoginComplete(BaseModel):
    code: SecretStr | None = None
    password: SecretStr | None = None


class TelegramConnectionScope(BaseModel):
    folder_ids: list[int] = Field(min_length=1, max_length=20)
    history_days: int = Field(default=14, ge=0, le=180)
    personal_dialogs_consent: bool = False


class TelegramSourcePreview(BaseModel):
    link: str = Field(min_length=5, max_length=500)


class TelegramSourceConfirm(BaseModel):
    preview_job_id: str
    selected_peer_ids: list[str] = Field(min_length=1, max_length=100)
    join: bool = False


ClientOnboardingStep = Literal[
    "welcome",
    "telegram_connection",
    "monitoring_started",
    "reports",
    "groups",
    "notifications",
    "mini_guide",
    "employees",
    "final_review",
    "completed",
]


class ClientOnboardingPatch(BaseModel):
    step: ClientOnboardingStep
    status: Literal["completed", "skipped"] = "completed"


CLIENT_ONBOARDING_STEPS: tuple[ClientOnboardingStep, ...] = (
    "welcome",
    "telegram_connection",
    "monitoring_started",
    "reports",
    "groups",
    "notifications",
    "mini_guide",
    "employees",
    "final_review",
    "completed",
)


async def get_client_connection_service(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> TelegramConnectionService:
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    settings = await load_runtime_secret_overrides(factory, settings)
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram user-session integration is not configured",
        )
    return TelegramConnectionService(
        factory,
        EncryptionService(settings.app_encryption_key.get_secret_value()),
        TelethonGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            device_model=settings.telegram_device_model,
            system_version=settings.telegram_system_version,
            app_version=settings.telegram_app_version,
            lang_code=settings.telegram_lang_code,
            system_lang_code=settings.telegram_system_lang_code,
        ),
    )


def require_permission(context: ClientAuthContext, permission: str) -> None:
    if not context.allows(permission):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")


def can_read_problem(context: ClientAuthContext, problem: OperationalProblem) -> bool:
    if context.membership.role in {"owner", "manager"} or context.allows("problems.read_all"):
        return True
    return bool(
        context.membership.employee_id
        and problem.responsible_employee_id == context.membership.employee_id
        and context.allows("problems.read_own")
    )


def can_manage_problem(context: ClientAuthContext, problem: OperationalProblem) -> bool:
    if context.membership.role in {"owner", "manager"} or context.allows("problems.manage"):
        return True
    return bool(
        context.membership.employee_id
        and problem.responsible_employee_id == context.membership.employee_id
        and context.allows("problems.manage_own")
    )


def job_payload(job: BackgroundJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "type": job.job_type,
        "status": "retry" if job.status == "retry_scheduled" else job.status,
        "stage": job.progress_json.get("stage"),
        "progress": job.progress_json,
        "scheduled_at": job.scheduled_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "attempt": job.attempts,
        "max_attempts": job.max_attempts,
        "delay_reason": job.delay_reason,
        "last_error": job.last_error,
        "result": job.result_json,
    }


async def record_event(
    session: AsyncSession,
    context: ClientAuthContext,
    event_name: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        ProductEvent(
            tenant_id=context.tenant.id,
            bot_instance_id=context.bot.id,
            telegram_user_id=context.membership.telegram_user_id,
            event_name=event_name,
            metadata_json=metadata or {},
        )
    )


def validate_webapp_init_data(
    init_data: str, bot_token: str, *, now: int | None = None, max_age_seconds: int = 86_400
) -> dict[str, Any] | None:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    current = int(time.time()) if now is None else now
    if auth_date > current + 60 or current - auth_date > max_age_seconds:
        return None
    return {"user_id": user_id, "auth_date": auth_date, "user": user}


@dataclass(frozen=True, slots=True)
class ClientAuthContext:
    tenant: Tenant
    bot: BotInstance
    membership: TenantMembership
    permissions: frozenset[str]
    telegram_user: dict[str, Any]

    def allows(self, permission: str) -> bool:
        return self.membership.role == "owner" or permission in self.permissions


async def require_client_context(
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> ClientAuthContext:
    if not authorization or not authorization.lower().startswith("tma "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram Mini App authentication required",
        )
    init_data = authorization[4:]
    encryption = EncryptionService(settings.app_encryption_key.get_secret_value())
    bots = list(
        await session.scalars(
            select(BotInstance).where(
                BotInstance.enabled.is_(True),
                BotInstance.deleted_at.is_(None),
            )
        )
    )
    for bot in bots:
        secret = await session.get(EncryptedSecret, bot.secret_id)
        if secret is None or secret.deleted_at is not None:
            continue
        token = encryption.decrypt(secret.ciphertext)
        validated = validate_webapp_init_data(init_data, token)
        token = ""
        if validated is None:
            continue
        tenant = await session.get(Tenant, bot.tenant_id)
        if tenant is None or tenant.deleted_at is not None:
            continue
        membership = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant.id,
                TenantMembership.telegram_user_id == validated["user_id"],
                TenantMembership.status == "active",
            )
        )
        if membership is None:
            membership = await claim_employee_by_username(
                session,
                tenant_id=tenant.id,
                telegram_user_id=validated["user_id"],
                telegram_username=validated["user"].get("username"),
            )
            if membership is not None:
                await session.commit()
        if membership is None:
            continue
        permissions = frozenset(
            await session.scalars(
                select(Permission.permission).where(
                    Permission.tenant_id == tenant.id,
                    Permission.membership_id == membership.id,
                )
            )
        )
        return ClientAuthContext(tenant, bot, membership, permissions, validated["user"])
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Mini App authentication"
    )


ClientContext = Annotated[ClientAuthContext, Depends(require_client_context)]


async def mini_app_dashboard_summary(
    session: AsyncSession, context: ClientAuthContext
) -> dict[str, Any]:
    tenant_id = context.tenant.id
    employee_id = context.membership.employee_id
    self_scoped = context.membership.role == "employee" and not context.allows("problems.read_all")
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    )
    critical_threshold = settings.signal_immediate_threshold if settings else 85
    counts: dict[str, Any] = {
        "problems": int(
            await session.scalar(
                select(func.count(OperationalProblem.id)).where(
                    OperationalProblem.tenant_id == tenant_id,
                    OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                    *(
                        (OperationalProblem.responsible_employee_id == employee_id,)
                        if self_scoped
                        else ()
                    ),
                )
            )
            or 0
        ),
        "signals": int(
            await session.scalar(
                select(func.count(Signal.id)).where(
                    Signal.tenant_id == tenant_id,
                    Signal.criticality >= critical_threshold,
                    Signal.status.in_(("triaged", "problem_created")),
                    *((Signal.employee_id == employee_id,) if self_scoped else ()),
                )
            )
            or 0
        ),
        "commitments": int(
            await session.scalar(
                select(func.count(Commitment.id)).where(
                    Commitment.tenant_id == tenant_id,
                    Commitment.status == "open",
                    *((Commitment.responsible_employee_id == employee_id,) if self_scoped else ()),
                )
            )
            or 0
        ),
        "reports": int(
            await session.scalar(select(func.count(Report.id)).where(Report.tenant_id == tenant_id))
            or 0
        ),
        "employees": int(
            await session.scalar(
                select(func.count(Employee.id)).where(
                    Employee.tenant_id == tenant_id,
                    Employee.status == "active",
                    *((Employee.id == employee_id,) if self_scoped else ()),
                )
            )
            or 0
        ),
        "connections": int(
            await session.scalar(
                select(func.count(TelegramConnection.id)).where(
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.deleted_at.is_(None),
                )
            )
            or 0
        ),
        "groups": int(
            await session.scalar(
                select(func.count(GroupIntegration.id)).where(
                    GroupIntegration.tenant_id == tenant_id
                )
            )
            or 0
        ),
    }
    day_start = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
    calls = int(
        await session.scalar(
            select(func.count(AIUsageCall.id)).where(
                AIUsageCall.tenant_id == tenant_id,
                AIUsageCall.occurred_at >= day_start,
            )
        )
        or 0
    )
    counts["ai_usage"] = {
        "calls_today": calls,
    }
    return counts


def client_onboarding_payload(settings: TenantSettings | None) -> dict[str, Any]:
    completed = bool(settings and settings.client_onboarding_completed_at)
    step = (
        "completed" if completed else (settings.client_onboarding_step if settings else "welcome")
    )
    if step not in CLIENT_ONBOARDING_STEPS:
        step = "welcome"
    return {
        "step": step,
        "completed": completed,
        "completed_at": settings.client_onboarding_completed_at if settings else None,
        "steps": list(CLIENT_ONBOARDING_STEPS),
        "statuses": {
            key: value
            for key, value in dict(settings.client_onboarding_json if settings else {}).items()
            if key in CLIENT_ONBOARDING_STEPS and value in {"completed", "skipped"}
        },
    }


async def reconcile_connected_onboarding(
    session: AsyncSession,
    settings: TenantSettings | None,
    connection: TelegramConnection | None,
) -> bool:
    """Recover onboarding when Telegram login finished before the UI advanced.

    A Mini App can be closed or suspended by iOS immediately after Telegram
    accepts the code/2FA. The connection is already durable at that point, so a
    persisted ``telegram_connection`` step must resume at monitoring instead of
    showing an endless local syncing state.
    """
    if (
        settings is None
        or settings.client_onboarding_completed_at is not None
        or settings.client_onboarding_step != "telegram_connection"
        or connection is None
        or connection.status not in {"connected", "syncing", "ready"}
    ):
        return False
    states = dict(settings.client_onboarding_json or {})
    states["telegram_connection"] = "completed"
    settings.client_onboarding_json = states
    settings.client_onboarding_step = "monitoring_started"
    await session.commit()
    return True


@router.post("/mini-app/auth")
async def mini_app_auth(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    connection = await TenantClientRepository(session, context.tenant.id).current_connection()
    await reconcile_connected_onboarding(session, settings, connection)
    permissions = ["*"] if context.membership.role == "owner" else sorted(context.permissions)
    return {
        "tenant_id": context.tenant.id,
        "tenant_name": context.tenant.name,
        "user": {
            "telegram_user_id": context.membership.telegram_user_id,
            "first_name": context.telegram_user.get("first_name"),
            "last_name": context.telegram_user.get("last_name"),
            "username": context.telegram_user.get("username"),
            "role": context.membership.role,
        },
        "permissions": permissions,
        "project_context": {
            "status": context.tenant.status,
            "timezone": settings.timezone if settings else None,
            "client_bot": {"id": context.bot.id, "username": context.bot.username},
            "onboarding_state": onboarding_state(connection),
            "onboarding": client_onboarding_payload(settings),
        },
        "dashboard_summary": await mini_app_dashboard_summary(session, context),
    }


@router.patch("/onboarding")
async def update_client_onboarding(
    payload: ClientOnboardingPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant settings are not configured",
        )
    if settings.client_onboarding_completed_at is not None:
        return client_onboarding_payload(settings)
    current = (
        settings.client_onboarding_step
        if settings.client_onboarding_step in CLIENT_ONBOARDING_STEPS
        else "welcome"
    )
    if CLIENT_ONBOARDING_STEPS.index(payload.step) < CLIENT_ONBOARDING_STEPS.index(current):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Onboarding cannot move backwards",
        )
    settings.client_onboarding_step = payload.step
    states = dict(settings.client_onboarding_json or {})
    states[current] = payload.status
    settings.client_onboarding_json = states
    if payload.step == "completed":
        settings.client_onboarding_completed_at = datetime.now(UTC)
        states["completed"] = "completed"
        settings.client_onboarding_json = states
    await session.commit()
    return client_onboarding_payload(settings)


def onboarding_state(connection: TelegramConnection | None) -> str:
    if connection is None or connection.status == "disconnected":
        return "not_connected"
    if connection.status in {"awaiting_code", "awaiting_2fa"}:
        return "connecting"
    if connection.status in {"failed", "reauthorization_required"}:
        return "reauthorization_required"
    if connection.status == "syncing":
        return "synchronization"
    if connection.status == "ready":
        return "ready"
    if connection.progress_stage in {"personal_sources_enabled", "scope_selected"}:
        return "synchronization"
    if connection.progress_stage == "scope_selected":
        return "chat_selection"
    return "connecting"


@router.get("/bootstrap")
async def client_bootstrap(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    app_settings: Settings = Depends(get_settings),  # noqa: B008
) -> dict[str, Any]:
    tenant = context.tenant
    welcome_copy = fallback_welcome(tenant)
    if (
        tenant.settings.client_onboarding_completed_at is None
        and tenant.settings.client_onboarding_step == "welcome"
    ):
        factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
        runtime_settings = await load_runtime_secret_overrides(factory, app_settings)
        provider = None
        if runtime_settings.deepseek_api_key:
            provider = DeepSeekProvider(
                base_url=runtime_settings.deepseek_base_url,
                timeout_seconds=min(30, runtime_settings.ai_request_timeout_seconds),
                api_key_value=runtime_settings.deepseek_api_key.get_secret_value(),
            )
        welcome_copy = await ensure_onboarding_welcome(
            session,
            tenant,
            provider=provider,
            model=runtime_settings.deepseek_fast_model,
        )
    connections = list(
        await session.scalars(
            select(TelegramConnection)
            .where(
                TelegramConnection.tenant_id == tenant.id,
                TelegramConnection.deleted_at.is_(None),
                TelegramConnection.status.in_(VISIBLE_CONNECTION_STATUSES),
            )
            .order_by(TelegramConnection.created_at.desc())
        )
    )
    connection = next(
        (item for item in connections if item.status == "ready"),
        connections[0] if connections else None,
    )
    run = await session.scalar(
        select(InitialAnalysisRun)
        .where(InitialAnalysisRun.tenant_id == tenant.id)
        .order_by(InitialAnalysisRun.created_at.desc())
        .limit(1)
    )
    dialog_counts = dict(
        (
            await session.execute(
                select(TelegramDialog.dialog_type, func.count(TelegramDialog.id))
                .where(
                    TelegramDialog.tenant_id == tenant.id,
                    TelegramDialog.excluded.is_(False),
                )
                .group_by(TelegramDialog.dialog_type)
            )
        ).all()
    )
    employee_count = int(
        await session.scalar(
            select(func.count(Employee.id)).where(
                Employee.tenant_id == tenant.id,
                Employee.status == "active",
            )
        )
        or 0
    )
    group_count = int(
        await session.scalar(
            select(func.count(GroupIntegration.id)).where(
                GroupIntegration.tenant_id == tenant.id,
                GroupIntegration.status == "active",
            )
        )
        or 0
    )
    problems = list(
        await session.scalars(
            select(OperationalProblem)
            .where(
                OperationalProblem.tenant_id == tenant.id,
                OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
            )
            .order_by(OperationalProblem.occurred_at.desc())
            .limit(20)
        )
    )
    state = onboarding_state(connection)
    menus = {
        "not_connected": ["Подключить Telegram"],
        "connecting": ["Продолжить подключение"],
        "folder_selection": ["Выбрать папку"],
        "chat_selection": ["Выбор чатов", "Подключения", "Настройки анализа"],
        "synchronization": ["Прогресс", "Найденные данные", "Остановить анализ"],
        "ready": [
            "Сводка",
            "Важное",
            "Диалоги",
            "Сотрудники",
            "Отчёты",
            "Подключения",
            "Настройки",
        ],
        "reauthorization_required": ["Повторная авторизация", "Безопасность", "Поддержка"],
    }
    return {
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "owner_name": tenant.owner_name,
            "owner_username": tenant.owner_telegram_username,
            "niche": tenant.niche,
            "business_description": tenant.business_description,
            "target_audience": tenant.target_audience,
            "monitoring_priorities": list(tenant.ai_profile.critical_events or []),
            "welcome": welcome_copy.model_dump(mode="json"),
        },
        "role": context.membership.role,
        "permissions": sorted(context.permissions),
        "onboarding_state": state,
        "onboarding": client_onboarding_payload(tenant.settings),
        "menu": menus[state],
        "connection": None
        if connection is None
        else {
            "status": connection.status,
            "account": connection.display_name or connection.phone_masked,
            "folder": connection.selected_folder_title,
            "history_days": connection.history_days,
            "personal_dialogs_consent": connection.personal_dialogs_consent,
        },
        "connections": [
            {
                "id": item.id,
                "status": item.status,
                "account": item.display_name or item.phone_masked,
                "health_status": item.health_status,
                "username": item.username,
                "last_incremental_sync_at": item.last_incremental_sync_at,
            }
            for item in connections
        ],
        "progress": None
        if run is None
        else {
            "status": run.status,
            "stage": run.stage,
            "percent": run.progress_percent,
            "dialogs_total": run.total_dialogs,
            "dialogs_completed": run.completed_dialogs + run.failed_dialogs,
            "failed_dialogs": run.failed_dialogs,
            "messages_loaded": run.messages_loaded,
            "metrics": run.metrics_json,
        },
        "dialog_counts": dialog_counts,
        "employee_count": employee_count,
        "group_count": group_count,
        "problems": [
            {
                "id": problem.id,
                "type": problem.problem_type,
                "priority": problem.priority,
                "confidence": problem.confidence,
                "evidence": problem.evidence,
                "explanation": problem.explanation,
                "recommended_action": problem.recommended_action,
                "status": problem.status,
                "responsible_employee_id": problem.responsible_employee_id,
                "deadline_at": problem.deadline_at,
                "occurred_at": problem.occurred_at,
            }
            for problem in problems
            if can_read_problem(context, problem)
        ],
    }


@router.get("/dashboard")
async def dashboard(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    repository = TenantClientRepository(session, context.tenant.id)
    metrics = await repository.latest_metrics()
    run = await repository.current_analysis()
    job = await repository.current_job()
    tenant_settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    summary = {
        "open_problems": int(
            await session.scalar(
                select(func.count(OperationalProblem.id)).where(
                    OperationalProblem.tenant_id == context.tenant.id,
                    OperationalProblem.status.in_(ACTIVE_PROBLEM_STATUSES),
                )
            )
            or 0
        ),
        "critical_signals": int(
            await session.scalar(
                select(func.count(Signal.id)).where(
                    Signal.tenant_id == context.tenant.id,
                    Signal.criticality >= tenant_settings.signal_immediate_threshold,
                )
            )
            or 0
        ),
        "open_commitments": int(
            await session.scalar(
                select(func.count(Commitment.id)).where(
                    Commitment.tenant_id == context.tenant.id,
                    Commitment.status == "open",
                )
            )
            or 0
        ),
        "active_connections": int(
            await session.scalar(
                select(func.count(TelegramConnection.id)).where(
                    TelegramConnection.tenant_id == context.tenant.id,
                    TelegramConnection.deleted_at.is_(None),
                    TelegramConnection.status.in_(("connected", "ready")),
                )
            )
            or 0
        ),
    }
    return {
        "tenant": {"id": context.tenant.id, "name": context.tenant.name},
        "summary": summary,
        "metrics": metrics.metrics_json if metrics else {},
        "analysis": None
        if run is None
        else {
            "id": run.id,
            "status": run.status,
            "stage": run.stage,
            "report_due_at": run.report_due_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "delayed_reason": run.delayed_reason,
        },
        "current_job": job_payload(job),
    }


@router.get("/problems")
async def problems(
    context: ClientContext,
    problem_status: Annotated[str | None, Query(alias="status")] = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    repository = TenantClientRepository(session, context.tenant.id)
    rows = await repository.problems()
    if problem_status:
        rows = [item for item in rows if item.status == problem_status]
    else:
        rows = [item for item in rows if item.status in ACTIVE_PROBLEM_STATUSES]
    rows = [item for item in rows if can_read_problem(context, item)]
    employee_ids = {item.responsible_employee_id for item in rows if item.responsible_employee_id}
    connection_ids = {item.connection_id for item in rows if item.connection_id}
    dialog_ids = {item.dialog_id for item in rows if item.dialog_id}
    employee_map = (
        {
            item.id: item
            for item in await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        }
        if employee_ids
        else {}
    )
    connection_map = (
        {
            item.id: item
            for item in await session.scalars(
                select(TelegramConnection).where(TelegramConnection.id.in_(connection_ids))
            )
        }
        if connection_ids
        else {}
    )
    dialog_map = (
        {
            item.id: item
            for item in await session.scalars(
                select(TelegramDialog).where(TelegramDialog.id.in_(dialog_ids))
            )
        }
        if dialog_ids
        else {}
    )
    return [
        {
            "id": item.id,
            "type": item.problem_type,
            "status": item.status,
            "priority": item.priority,
            "confidence": item.confidence,
            "evidence": item.evidence,
            "explanation": item.explanation,
            "recommended_action": item.recommended_action,
            "responsible_employee_id": item.responsible_employee_id,
            "responsible_employee_name": (
                employee_map[item.responsible_employee_id].display_name
                if item.responsible_employee_id in employee_map
                else None
            ),
            "connection_name": (
                connection_map[item.connection_id].display_name
                or connection_map[item.connection_id].username
                or connection_map[item.connection_id].phone_masked
                if item.connection_id in connection_map
                else None
            ),
            "dialog_username": (
                dialog_map[item.dialog_id].username if item.dialog_id in dialog_map else None
            ),
            "deadline_at": item.deadline_at,
            "occurred_at": item.occurred_at,
        }
        for item in rows
    ]


@router.get("/problems/{problem_id}")
async def problem_detail(
    problem_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    item = await TenantClientRepository(session, context.tenant.id).problem(problem_id)
    if item is None or not can_read_problem(context, item):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Problem not found")
    transitions = list(
        await session.scalars(
            select(ProblemTransition)
            .where(
                ProblemTransition.tenant_id == context.tenant.id,
                ProblemTransition.problem_id == item.id,
            )
            .order_by(ProblemTransition.occurred_at)
        )
    )
    verifications = list(
        await session.scalars(
            select(ProblemVerification)
            .where(
                ProblemVerification.tenant_id == context.tenant.id,
                ProblemVerification.problem_id == item.id,
            )
            .order_by(ProblemVerification.checked_at.desc())
        )
    )
    source = await session.get(TelegramMessage, item.source_message_id)
    context_messages: list[TelegramMessage] = []
    if source is not None:
        before = list(
            await session.scalars(
                select(TelegramMessage)
                .where(
                    TelegramMessage.dialog_id == item.dialog_id,
                    TelegramMessage.telegram_message_id < source.telegram_message_id,
                    TelegramMessage.deleted_at.is_(None),
                )
                .order_by(TelegramMessage.telegram_message_id.desc())
                .limit(4)
            )
        )
        after = list(
            await session.scalars(
                select(TelegramMessage)
                .where(
                    TelegramMessage.dialog_id == item.dialog_id,
                    TelegramMessage.telegram_message_id > source.telegram_message_id,
                    TelegramMessage.deleted_at.is_(None),
                )
                .order_by(TelegramMessage.telegram_message_id)
                .limit(4)
            )
        )
        context_messages = [*reversed(before), source, *after]
    responsible = (
        await session.get(Employee, item.responsible_employee_id)
        if item.responsible_employee_id
        else None
    )
    connection = await session.get(TelegramConnection, item.connection_id)
    dialog = await session.get(TelegramDialog, item.dialog_id)
    await record_event(session, context, "problem_opened", {"problem_id": item.id})
    await session.commit()
    return {
        "id": item.id,
        "type": item.problem_type,
        "status": item.status,
        "priority": item.priority,
        "confidence": item.confidence,
        "evidence": item.evidence,
        "explanation": item.explanation,
        "recommended_action": item.recommended_action,
        "responsible_employee_id": item.responsible_employee_id,
        "responsible_employee_name": responsible.display_name if responsible else None,
        "connection_name": (
            connection.display_name or connection.username or connection.phone_masked
            if connection
            else None
        ),
        "connection_username": connection.username if connection else None,
        "dialog_title": dialog.title if dialog else None,
        "dialog_username": dialog.username if dialog else None,
        "deadline_at": item.deadline_at,
        "closed_reason": item.closed_reason,
        "resolution_evidence": item.resolution_evidence,
        "occurred_at": item.occurred_at,
        "context_messages": [
            {
                "id": message.id,
                "text": message.body_text,
                "outgoing": message.outgoing,
                "sender_role": message.sender_role,
                "sent_at": message.sent_at,
                "is_source": message.id == item.source_message_id,
            }
            for message in context_messages
        ],
        "transitions": [
            {
                "from_status": transition.from_status,
                "to_status": transition.to_status,
                "actor_type": transition.actor_type,
                "actor_id": transition.actor_id,
                "reason": transition.reason,
                "evidence": transition.evidence,
                "occurred_at": transition.occurred_at,
            }
            for transition in transitions
        ],
        "verifications": [
            {
                "outcome": verification.outcome,
                "confidence": verification.confidence,
                "method": verification.method,
                "reason": verification.reason,
                "evidence_message_ids": verification.evidence_message_ids_json,
                "checked_at": verification.checked_at,
            }
            for verification in verifications
        ],
    }


@router.patch("/problems/{problem_id}")
async def update_problem(
    problem_id: str,
    payload: ProblemPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    item = await TenantClientRepository(session, context.tenant.id).problem(problem_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Problem not found")
    if not can_manage_problem(context, item):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    if (
        context.membership.role == "employee"
        and payload.responsible_employee_id is not None
        and payload.responsible_employee_id != context.membership.employee_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee cannot reassign a problem to another employee",
        )
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    lifecycle = ProblemLifecycleService(factory)
    try:
        updated = await lifecycle.transition(
            context.tenant.id,
            problem_id,
            TransitionRequest(
                target=payload.status,
                actor_type="membership",
                actor_id=context.membership.id,
                reason=payload.reason,
                evidence=payload.evidence,
                responsible_employee_id=payload.responsible_employee_id,
                deadline_at=payload.deadline_at,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    event = f"problem_{updated.status}"
    async with factory() as event_session:
        await record_event(event_session, context, event, {"problem_id": updated.id})
        await event_session.commit()
    return {
        "id": updated.id,
        "status": updated.status,
        "responsible_employee_id": updated.responsible_employee_id,
        "deadline_at": updated.deadline_at,
    }


@router.get("/reports")
async def reports(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    if context.membership.role not in {"owner", "manager"} and not context.allows("reports.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    rows = await TenantClientRepository(session, context.tenant.id).reports()
    canonical_rows: list[Report] = []
    seen: set[tuple[object, str]] = set()
    for item in rows:
        key = (item.period_end.date(), item.summary.strip())
        if key in seen or item.summary.strip() == "Обработано сообщений: 0. Проблем: 0.":
            continue
        seen.add(key)
        canonical_rows.append(item)
    return [
        {
            "id": item.id,
            "status": item.status,
            "period_start": item.period_start,
            "period_end": item.period_end,
            "due_at": item.due_at,
            "ready_at": item.ready_at,
            "delivery_status": item.delivery_status,
            "summary": item.summary,
        }
        for item in canonical_rows
    ]


@router.get("/reports/{report_id}")
async def report_detail(
    report_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    if context.membership.role not in {"owner", "manager"} and not context.allows("reports.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    repository = TenantClientRepository(session, context.tenant.id)
    report = await repository.report(report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    sections = list(
        await session.scalars(
            select(ReportSection)
            .where(
                ReportSection.tenant_id == context.tenant.id,
                ReportSection.report_id == report.id,
            )
            .order_by(ReportSection.position)
        )
    )
    metrics = list(
        await session.scalars(
            select(ReportMetric).where(
                ReportMetric.tenant_id == context.tenant.id,
                ReportMetric.report_id == report.id,
            )
        )
    )
    problem_ids = list(
        await session.scalars(
            select(ReportProblem.problem_id).where(
                ReportProblem.tenant_id == context.tenant.id,
                ReportProblem.report_id == report.id,
            )
        )
    )
    await record_event(session, context, "report_opened", {"report_id": report.id})
    await session.commit()
    return {
        "id": report.id,
        "status": report.status,
        "summary": report.summary,
        "period": {"start": report.period_start, "end": report.period_end},
        "sections": [
            {"key": item.section_key, "position": item.position, "data": item.data_json}
            for item in sections
        ],
        "metrics": {item.metric_key: item.numeric_value for item in metrics},
        "problem_ids": problem_ids,
    }


@router.get("/connections")
async def connections(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(TelegramConnection)
            .where(
                TelegramConnection.tenant_id == context.tenant.id,
                TelegramConnection.deleted_at.is_(None),
                TelegramConnection.status.in_(VISIBLE_CONNECTION_STATUSES),
            )
            .order_by(TelegramConnection.created_at.desc())
        )
    )
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result: list[dict[str, Any]] = []
    for item in rows:
        employee = (
            await session.get(Employee, item.assigned_employee_id)
            if item.assigned_employee_id
            else None
        )
        personal_dialogs = int(
            await session.scalar(
                select(func.count(TelegramDialog.id)).where(
                    TelegramDialog.connection_id == item.id,
                    TelegramDialog.dialog_type == "personal",
                    TelegramDialog.excluded.is_(False),
                )
            )
            or 0
        )
        new_contacts_today = int(
            await session.scalar(
                select(func.count(TelegramDialog.id)).where(
                    TelegramDialog.connection_id == item.id,
                    TelegramDialog.dialog_type == "personal",
                    TelegramDialog.created_at >= day_start,
                )
            )
            or 0
        )
        messages_today = int(
            await session.scalar(
                select(func.count(TelegramMessage.id)).where(
                    TelegramMessage.connection_id == item.id,
                    TelegramMessage.sent_at >= day_start,
                    TelegramMessage.deleted_at.is_(None),
                )
            )
            or 0
        )
        result.append(
            {
                "id": item.id,
                "status": item.status,
                "account": item.display_name or item.phone_masked,
                "username": item.username,
                "health_status": item.health_status,
                "last_health_check_at": item.last_health_check_at,
                "last_sync_at": item.last_sync_at,
                "folder": item.selected_folder_title,
                "employee_id": item.assigned_employee_id,
                "employee_name": employee.display_name if employee else None,
                "personal_dialogs": personal_dialogs,
                "new_contacts_today": new_contacts_today,
                "messages_today": messages_today,
                **login_delivery_payload(item),
            }
        )
    return result


def login_delivery_payload(connection: TelegramConnection) -> dict[str, Any]:
    metadata = (getattr(connection, "progress_json", None) or {}).get("login_code", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "code_delivery_method": metadata.get("delivery_type") or "telegram_app",
        "next_code_delivery_method": metadata.get("next_delivery_type"),
        "resend_available_in": (
            TelegramConnectionService.resend_available_in(connection)
            if hasattr(connection, "progress_json")
            else 0
        ),
    }


@router.post("/connections/login/start", status_code=status.HTTP_201_CREATED)
async def start_connection_login(
    payload: TelegramLoginStart,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    employee = None
    if payload.employee_id:
        employee = await session.scalar(
            select(Employee).where(
                Employee.id == payload.employee_id,
                Employee.tenant_id == context.tenant.id,
            )
        )
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")
    try:
        connection = await connection_service.begin_login(
            context.tenant.id,
            payload.phone,
            assigned_employee_id=employee.id if employee else None,
        )
        if payload.create_employee:
            connection.progress_json = {**(connection.progress_json or {}), "create_employee": True}
            session.add(connection)
            await session.commit()
    except TelegramFloodWait as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram просит подождать {exc.retry_after_seconds} сек. перед новой попыткой.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Проверьте формат номера телефона.") from exc
    except Exception as exc:
        error_name = type(exc).__name__
        detail = {
            "PhoneNumberInvalidError": "Telegram не принял номер телефона.",
            "PhoneNumberBannedError": "Этот Telegram-аккаунт заблокирован.",
            "TimeoutError": "Telegram не ответил вовремя. Проверьте сеть и попробуйте ещё раз.",
        }.get(error_name, "Telegram временно не принял запрос. Попробуйте позже.")
        raise HTTPException(status_code=502, detail=detail) from exc
    return {
        "id": connection.id,
        "status": connection.status,
        "phone_masked": connection.phone_masked,
        "employee_id": employee.id if employee else None,
        **login_delivery_payload(connection),
    }


@router.post("/connections/{connection_id}/login/resend")
async def resend_connection_login(
    connection_id: str,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    try:
        connection = await connection_service.resend_login(context.tenant.id, connection_id)
    except TelegramFloodWait as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram просит подождать {exc.retry_after_seconds} сек. перед новой попыткой.",
        ) from exc
    except TelegramConnectionError as exc:
        message = str(exc)
        if message.startswith("resend cooldown:"):
            remaining = message.rsplit(":", 1)[-1]
            raise HTTPException(
                status_code=429,
                detail=f"Новый код можно запросить через {remaining} сек.",
            ) from exc
        raise HTTPException(
            status_code=409,
            detail="Запрос кода уже завершён. Начните подключение заново.",
        ) from exc
    except Exception as exc:
        error_name = type(exc).__name__
        detail = {
            "PhoneCodeExpiredError": "Предыдущий запрос истёк. Начните подключение заново.",
            "PhoneNumberInvalidError": "Telegram не принял номер телефона.",
            "SendCodeUnavailableError": "Telegram временно не может отправить новый код.",
        }.get(error_name, "Telegram временно не отправил новый код. Попробуйте через 30 секунд.")
        raise HTTPException(status_code=502, detail=detail) from exc
    return {
        "id": connection.id,
        "status": connection.status,
        "phone_masked": connection.phone_masked,
        **login_delivery_payload(connection),
    }


@router.post("/connections/{connection_id}/login/complete")
async def complete_connection_login(
    connection_id: str,
    payload: TelegramLoginComplete,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    code = payload.code.get_secret_value() if payload.code else None
    password = payload.password.get_secret_value() if payload.password else None
    if not code and not password:
        raise HTTPException(status_code=422, detail="Code or 2FA password is required")
    try:
        connection = await connection_service.complete_login(
            context.tenant.id,
            connection_id=connection_id,
            code=code,
            password=password,
        )
    except TelegramFloodWait as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram просит подождать {exc.retry_after_seconds} сек. перед новой попыткой.",
        ) from exc
    except TelegramSessionRevoked as exc:
        raise HTTPException(
            status_code=409, detail="Сессия Telegram отозвана. Подключите аккаунт заново."
        ) from exc
    except TelegramLoginRestarted as exc:
        raise HTTPException(
            status_code=409,
            detail="Сервис перезапускался во время входа. Начните подключение заново.",
        ) from exc
    except TelegramConnectionError as exc:
        raise HTTPException(
            status_code=409, detail="Код истёк или сессия входа завершена. Запросите новый код."
        ) from exc
    except Exception as exc:
        error_name = type(exc).__name__
        detail = {
            "PhoneCodeInvalidError": "Код не подошёл. Проверьте его и попробуйте ещё раз.",
            "PhoneCodeExpiredError": "Код истёк. Запросите новый код.",
            "PasswordHashInvalidError": "Пароль 2FA не подошёл. Попробуйте ещё раз.",
            "PhoneNumberInvalidError": "Telegram не принял номер телефона.",
        }.get(error_name, "Telegram не принял код или пароль 2FA. Попробуйте ещё раз.")
        raise HTTPException(status_code=422, detail=detail) from exc
    finally:
        code = password = None
    preparation_job_id = None
    if connection.status == "connected":
        tenant_settings = await session.scalar(
            select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
        )
        # Authorization is complete at this point. Catalog loading and initial
        # analysis are retryable background work and must never be reported as
        # an invalid Telegram code or 2FA password.
        preparation_job_id = await connection_service.queue.enqueue(
            "telegram.prepare_connection",
            {"history_days": tenant_settings.message_history_days if tenant_settings else 14},
            tenant_id=context.tenant.id,
            telegram_account_id=connection.id,
            idempotency_key=f"telegram-prepare:{connection.id}",
            priority=5,
            category="telegram_rpc",
            cost_class="light",
            max_attempts=8,
        )
        if (getattr(connection, "progress_json", None) or {}).get(
            "create_employee"
        ) and not getattr(connection, "assigned_employee_id", None):
            employee = await session.scalar(
                select(Employee).where(
                    Employee.tenant_id == context.tenant.id,
                    Employee.telegram_user_id == connection.telegram_user_id,
                )
            )
            if employee is None:
                employee = Employee(
                    tenant_id=context.tenant.id,
                    display_name=connection.display_name or connection.username or "Сотрудник",
                    telegram_user_id=connection.telegram_user_id,
                    telegram_username=connection.username,
                    role="employee",
                    status="active",
                    notifications_enabled=True,
                    criticality_threshold=85,
                )
                session.add(employee)
                await session.flush()
            else:
                employee.status = "active"
                employee.display_name = connection.display_name or employee.display_name
                employee.telegram_username = connection.username or employee.telegram_username
            connection.assigned_employee_id = employee.id
            await sync_employee_membership(session, employee)
        await reconcile_connected_onboarding(session, tenant_settings, connection)
        logger.info(
            "Telegram authorization completed tenant_id=%s connection_id=%s preparation_job_id=%s",
            context.tenant.id,
            connection.id,
            preparation_job_id,
        )
    return {
        "id": connection.id,
        "status": connection.status,
        "account": connection.display_name or connection.phone_masked,
        "username": connection.username,
        "requires_2fa": connection.status == "awaiting_2fa",
        "analysis_run_id": None,
        "preparation_job_id": preparation_job_id,
    }


@router.post("/connections/{connection_id}/catalog")
async def refresh_connection_catalog(
    connection_id: str,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    try:
        connection = await connection_service.refresh_catalog(context.tenant.id, connection_id)
        folders = await connection_service.list_folders(context.tenant.id, connection_id)
    except TelegramConnectionError as exc:
        raise HTTPException(
            status_code=409, detail="Connected Telegram session is required"
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram folders could not be loaded") from exc
    return {
        "connection_id": connection.id,
        "folders": [
            {"id": item.telegram_folder_id, "title": item.title, "chat_count": item.chat_count}
            for item in folders
        ],
    }


@router.post("/connections/{connection_id}/scope")
async def configure_connection_scope(
    connection_id: str,
    payload: TelegramConnectionScope,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    try:
        connection = await connection_service.select_scope(
            context.tenant.id,
            payload.folder_ids,
            personal_dialogs_consent=payload.personal_dialogs_consent,
            history_days=payload.history_days,
            connection_id=connection_id,
        )
        run = await connection_service.start_initial_sync(
            context.tenant.id,
            connection_id=connection.id,
        )
    except (TelegramConnectionError, LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Telegram analysis scope") from exc
    return {
        "connection_id": connection.id,
        "status": connection.status,
        "folder": connection.selected_folder_title,
        "history_days": connection.history_days,
        "analysis_run_id": run.id,
        "analysis_status": run.status,
    }


async def tenant_connection(
    session: AsyncSession, tenant_id: str, connection_id: str
) -> TelegramConnection:
    connection = await session.scalar(
        select(TelegramConnection).where(
            TelegramConnection.id == connection_id,
            TelegramConnection.tenant_id == tenant_id,
            TelegramConnection.deleted_at.is_(None),
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Telegram connection not found")
    if connection.status not in {"connected", "syncing", "ready"}:
        raise HTTPException(status_code=409, detail="Telegram connection is not ready")
    return connection


@router.get("/connections/{connection_id}/sources")
async def list_connection_sources(
    connection_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    require_permission(context, "employees.manage")
    await tenant_connection(session, context.tenant.id, connection_id)
    rows = list(
        await session.scalars(
            select(MonitoredSource)
            .where(
                MonitoredSource.tenant_id == context.tenant.id,
                MonitoredSource.connection_id == connection_id,
            )
            .order_by(MonitoredSource.created_at.asc())
        )
    )
    return [
        {
            "id": item.id,
            "canonical_peer_id": item.canonical_peer_id,
            "type": item.source_type,
            "title": item.title,
            "enabled": item.enabled,
            "added_via": item.added_via,
            "metadata": item.metadata_json,
        }
        for item in rows
    ]


@router.post("/connections/{connection_id}/sources/preview", status_code=202)
async def preview_connection_source(
    connection_id: str,
    payload: TelegramSourcePreview,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    require_permission(context, "employees.manage")
    await tenant_connection(session, context.tenant.id, connection_id)
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    job_id = await SQLiteJobQueue(factory).enqueue(
        "telegram.preview_source",
        {"link": payload.link},
        tenant_id=context.tenant.id,
        telegram_account_id=connection_id,
        priority=10,
        category="telegram_rpc",
        cost_class="light",
        max_attempts=3,
    )
    return {"job_id": job_id}


@router.post("/connections/{connection_id}/sources/confirm", status_code=202)
async def confirm_connection_sources(
    connection_id: str,
    payload: TelegramSourceConfirm,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    require_permission(context, "employees.manage")
    await tenant_connection(session, context.tenant.id, connection_id)
    preview_job = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.id == payload.preview_job_id,
            BackgroundJob.tenant_id == context.tenant.id,
            BackgroundJob.telegram_account_id == connection_id,
            BackgroundJob.job_type == "telegram.preview_source",
            BackgroundJob.status == "completed",
        )
    )
    if preview_job is None or not preview_job.result_json:
        raise HTTPException(status_code=409, detail="Source preview is not ready")
    available = {
        str(item.get("canonical_peer_id")) for item in preview_job.result_json.get("peers", [])
    }
    selected = list(dict.fromkeys(payload.selected_peer_ids))
    if not set(selected).issubset(available):
        raise HTTPException(status_code=422, detail="Unknown source selection")
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    job_id = await SQLiteJobQueue(factory).enqueue(
        "telegram.confirm_sources",
        {
            "preview": preview_job.result_json,
            "selected_peer_ids": selected,
            "join": payload.join,
        },
        tenant_id=context.tenant.id,
        telegram_account_id=connection_id,
        priority=10,
        category="telegram_rpc",
        cost_class="light",
        max_attempts=3,
    )
    return {"job_id": job_id}


@router.post("/connections/{connection_id}/login/cancel")
async def cancel_connection_login(
    connection_id: str,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
) -> dict[str, bool]:
    require_permission(context, "employees.manage")
    await connection_service.cancel_login(context.tenant.id, connection_id)
    return {"cancelled": True}


@router.get("/signals")
async def signals(
    context: ClientContext,
    signal_status: Annotated[str | None, Query(alias="status")] = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    query = select(Signal).where(Signal.tenant_id == context.tenant.id)
    if context.membership.role == "employee" and not context.allows("problems.read_all"):
        query = query.where(Signal.employee_id == context.membership.employee_id)
    if signal_status:
        query = query.where(Signal.status == signal_status)
    rows = list(await session.scalars(query.order_by(Signal.detected_at.desc()).limit(200)))
    return [
        {
            "id": item.id,
            "type": item.signal_type,
            "local_score": item.local_score,
            "ai_score": item.ai_score,
            "criticality": item.criticality,
            "status": item.status,
            "reason": item.reason,
            "detected_at": item.detected_at,
            "dialog_id": item.dialog_id,
            "connection_id": item.telegram_connection_id,
        }
        for item in rows
    ]


@router.get("/commitments")
async def commitments(
    context: ClientContext,
    commitment_status: Annotated[str | None, Query(alias="status")] = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    query = select(Commitment).where(Commitment.tenant_id == context.tenant.id)
    if context.membership.role == "employee" and not context.allows("commitments.read_all"):
        query = query.where(Commitment.responsible_employee_id == context.membership.employee_id)
    if commitment_status:
        query = query.where(Commitment.status == commitment_status)
    rows = list(await session.scalars(query.order_by(Commitment.deadline_at.asc()).limit(200)))
    return [
        {
            "id": item.id,
            "type": item.commitment_type,
            "status": item.status,
            "expected_action": item.expected_action,
            "deadline_at": item.deadline_at,
            "employee_id": item.responsible_employee_id,
            "dialog_id": item.dialog_id,
            "confidence": item.confidence,
            "linked_problem_id": await session.scalar(
                select(OperationalProblem.id).where(
                    OperationalProblem.tenant_id == context.tenant.id,
                    OperationalProblem.commitment_id == item.id,
                )
            ),
        }
        for item in rows
    ]


@router.patch("/commitments/{commitment_id}")
async def update_commitment(
    commitment_id: str,
    payload: CommitmentPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    item = await session.scalar(
        select(Commitment).where(
            Commitment.id == commitment_id,
            Commitment.tenant_id == context.tenant.id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commitment not found")
    can_manage_all = context.membership.role in {"owner", "manager"}
    can_manage_own = bool(
        context.membership.employee_id
        and item.responsible_employee_id == context.membership.employee_id
        and context.allows("commitments.manage_own")
    )
    if not can_manage_all and not can_manage_own:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    if item.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Commitment is not open")
    item.status = payload.status
    item.completed_at = datetime.now(UTC) if payload.status == "completed" else None
    item.metadata_json = {
        **item.metadata_json,
        "manual_resolution": {
            "status": payload.status,
            "reason": payload.reason,
            "membership_id": context.membership.id,
            "at": datetime.now(UTC).isoformat(),
        },
    }
    await record_event(
        session,
        context,
        "commitment_updated",
        {"commitment_id": item.id, "status": item.status},
    )
    await session.commit()
    return {"id": item.id, "status": item.status, "completed_at": item.completed_at}


@router.get("/employees")
async def employees(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    if context.membership.role == "observer" and not context.allows("employees.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    employee_filters = [Employee.tenant_id == context.tenant.id, Employee.status == "active"]
    if context.membership.role == "employee":
        employee_filters.append(Employee.id == context.membership.employee_id)
    rows = list(
        await session.scalars(
            select(Employee).where(*employee_filters).order_by(Employee.display_name)
        )
    )
    return [
        {
            "id": item.id,
            "name": item.display_name,
            "telegram_user_id": item.telegram_user_id,
            "telegram_username": item.telegram_username,
            "role": item.role,
            "status": item.status,
            "notifications_enabled": item.notifications_enabled,
            "criticality_threshold": item.criticality_threshold,
            "access_status": (
                await session.scalar(
                    select(TenantMembership.status).where(
                        TenantMembership.tenant_id == context.tenant.id,
                        TenantMembership.employee_id == item.id,
                    )
                )
            ),
            "connection_id": await session.scalar(
                select(TelegramConnection.id)
                .where(
                    TelegramConnection.tenant_id == context.tenant.id,
                    TelegramConnection.assigned_employee_id == item.id,
                    TelegramConnection.deleted_at.is_(None),
                    TelegramConnection.status.in_(
                        ("connected", "syncing", "ready", "reauthorization_required")
                    ),
                )
                .order_by(TelegramConnection.created_at.desc())
                .limit(1)
            ),
        }
        for item in rows
    ]


@router.delete("/employees/{employee_id}")
async def delete_employee(
    employee_id: str,
    context: ClientContext,
    connection_service: TelegramConnectionService = Depends(get_client_connection_service),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, bool]:
    require_permission(context, "employees.manage")
    employee = await session.scalar(
        select(Employee).where(
            Employee.id == employee_id,
            Employee.tenant_id == context.tenant.id,
            Employee.status == "active",
        )
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    connection_ids = list(
        await session.scalars(
            select(TelegramConnection.id).where(
                TelegramConnection.tenant_id == context.tenant.id,
                TelegramConnection.assigned_employee_id == employee.id,
                TelegramConnection.deleted_at.is_(None),
            )
        )
    )
    for connection_id in connection_ids:
        await connection_service.disconnect(context.tenant.id, connection_id)
    employee.status = "inactive"
    memberships = list(
        await session.scalars(
            select(TenantMembership).where(
                TenantMembership.tenant_id == context.tenant.id,
                TenantMembership.employee_id == employee.id,
            )
        )
    )
    for membership in memberships:
        membership.status = "revoked"
    await record_event(session, context, "employee_deleted", {"employee_id": employee.id})
    await session.commit()
    return {"deleted": True}


@router.post("/employees", status_code=status.HTTP_201_CREATED)
async def create_employee(
    payload: EmployeeCreate,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    employee = Employee(
        tenant_id=context.tenant.id,
        display_name=payload.display_name,
        telegram_user_id=payload.telegram_user_id,
        telegram_username=(payload.telegram_username or "").lstrip("@") or None,
        role=payload.role,
        notifications_enabled=payload.notifications_enabled,
        criticality_threshold=payload.criticality_threshold,
    )
    session.add(employee)
    await session.flush()
    try:
        membership = await sync_employee_membership(session, employee)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": employee.id,
        "name": employee.display_name,
        "membership_id": membership.id if membership else None,
        "access_status": membership.status if membership else "unlinked",
    }


@router.patch("/employees/{employee_id}")
async def update_employee(
    employee_id: str,
    payload: EmployeePatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "employees.manage")
    employee = await session.scalar(
        select(Employee).where(
            Employee.id == employee_id,
            Employee.tenant_id == context.tenant.id,
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    previous_telegram_user_id = employee.telegram_user_id
    values = payload.model_dump(exclude_unset=True)
    if "telegram_username" in values:
        values["telegram_username"] = (values["telegram_username"] or "").lstrip("@") or None
    for key, value in values.items():
        setattr(employee, key, value)
    try:
        membership = await sync_employee_membership(
            session,
            employee,
            previous_telegram_user_id=previous_telegram_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await record_event(session, context, "employee_updated", {"employee_id": employee.id})
    await session.commit()
    return {
        "id": employee.id,
        "name": employee.display_name,
        "telegram_user_id": employee.telegram_user_id,
        "telegram_username": employee.telegram_username,
        "role": employee.role,
        "status": employee.status,
        "notifications_enabled": employee.notifications_enabled,
        "criticality_threshold": employee.criticality_threshold,
        "access_status": membership.status if membership else "unlinked",
    }


@router.get("/group-integrations")
async def group_integrations(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(GroupIntegration)
            .where(GroupIntegration.tenant_id == context.tenant.id)
            .order_by(GroupIntegration.title)
        )
    )
    return [
        {
            "id": item.id,
            "telegram_chat_id": item.telegram_chat_id,
            "title": item.title,
            "status": item.status,
            "participants_count": item.participants_count,
            "notifications_enabled": item.notifications_enabled,
            "minimum_criticality": item.minimum_criticality,
            "reminder_cooldown_minutes": item.reminder_cooldown_minutes,
            "last_verified_at": item.last_verified_at,
        }
        for item in rows
    ]


@router.post("/group-integrations", status_code=status.HTTP_201_CREATED)
async def create_group_integration(
    payload: GroupIntegrationCreate,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "groups.manage")
    existing = await session.scalar(
        select(GroupIntegration).where(
            GroupIntegration.tenant_id == context.tenant.id,
            GroupIntegration.telegram_chat_id == payload.telegram_chat_id,
        )
    )
    if existing:
        return {"id": existing.id, "status": existing.status}
    group = GroupIntegration(
        tenant_id=context.tenant.id,
        bot_instance_id=context.bot.id,
        telegram_chat_id=payload.telegram_chat_id,
        title=payload.title,
        notifications_enabled=payload.notifications_enabled,
        minimum_criticality=payload.minimum_criticality,
        reminder_cooldown_minutes=payload.reminder_cooldown_minutes,
    )
    session.add(group)
    await session.commit()
    return {"id": group.id, "status": group.status}


@router.patch("/group-integrations/{group_id}")
async def update_group_integration(
    group_id: str,
    payload: GroupIntegrationPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "groups.manage")
    group = await session.scalar(
        select(GroupIntegration).where(
            GroupIntegration.id == group_id,
            GroupIntegration.tenant_id == context.tenant.id,
        )
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(group, key, value)
    await session.commit()
    return {
        "id": group.id,
        "status": group.status,
        "notifications_enabled": group.notifications_enabled,
        "minimum_criticality": group.minimum_criticality,
        "reminder_cooldown_minutes": group.reminder_cooldown_minutes,
    }


@router.get("/ai-usage")
async def ai_usage(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    day_start = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
    rows = (
        await session.execute(
            select(
                AIUsageCall.job_type,
                func.sum(AIUsageCall.input_tokens),
                func.sum(AIUsageCall.output_tokens),
                func.count(AIUsageCall.id),
            )
            .where(
                AIUsageCall.tenant_id == context.tenant.id,
                AIUsageCall.occurred_at >= day_start,
            )
            .group_by(AIUsageCall.job_type)
        )
    ).all()
    by_job = {
        str(job_type): {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "calls": int(calls or 0),
        }
        for job_type, input_tokens, output_tokens, calls in rows
    }
    return {
        "date": day_start.date(),
        "by_job_type": by_job,
        "total_tokens": sum(
            item["input_tokens"] + item["output_tokens"] for item in by_job.values()
        ),
    }


@router.get("/sync/current")
async def current_sync(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any] | None:
    return job_payload(await TenantClientRepository(session, context.tenant.id).current_job())


@router.get("/sync/{job_id}")
async def sync_job(
    job_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    job = await TenantClientRepository(session, context.tenant.id).job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job_payload(job) or {}


@router.post("/sync/start")
async def start_sync(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    require_permission(context, "analysis.start")
    schedule = await TenantClientRepository(session, context.tenant.id).schedule()
    if context.tenant.status != "active" or (schedule and schedule.access_status != "active"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Access is not active")
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    scheduler = TenantAnalysisScheduler(factory, SQLiteJobQueue(factory))
    job_id = await scheduler.trigger_now(context.tenant.id)
    await record_event(session, context, "manual_analysis_started", {"job_id": job_id})
    await session.commit()
    return {"job_id": job_id}


@router.post("/sync/cancel")
async def cancel_sync(
    job_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, bool]:
    require_permission(context, "analysis.cancel")
    job = await TenantClientRepository(session, context.tenant.id).job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    changed = await SQLiteJobQueue(factory).cancel(job.id, tenant_id=context.tenant.id)
    if changed:
        await record_event(session, context, "analysis_cancelled", {"job_id": job.id})
        await session.commit()
    return {"cancelled": changed}


@router.get("/settings")
async def get_client_settings(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    if context.membership.role not in {"owner", "manager"} and not context.allows("settings.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    schedule = await TenantClientRepository(session, context.tenant.id).schedule()
    return {
        "timezone": settings.timezone,
        "daily_report_time": settings.daily_report_time,
        "analysis_enabled": settings.analysis_enabled,
        "analysis_advance_minutes": settings.analysis_advance_minutes,
        "enabled_days": settings.enabled_days,
        "history_window_days": settings.history_window_days,
        "signal_report_threshold": settings.signal_report_threshold,
        "signal_problem_threshold": settings.signal_problem_threshold,
        "signal_immediate_threshold": settings.signal_immediate_threshold,
        "manager_notification_threshold": settings.manager_notification_threshold,
        "employee_notification_threshold": settings.employee_notification_threshold,
        "group_notification_threshold": settings.group_notification_threshold,
        "notification_immediate_threshold": settings.notification_immediate_threshold,
        "critical_fast_lane_rules": settings.critical_fast_lane_rules,
        "ai_daily_soft_limit": settings.ai_daily_soft_limit,
        "ai_daily_hard_limit": settings.ai_daily_hard_limit,
        "employee_notifications_enabled": settings.employee_notifications_enabled,
        "group_reminders_enabled": settings.group_reminders_enabled,
        "next_analysis_at": schedule.next_analysis_at if schedule else None,
    }


@router.patch("/settings")
async def patch_client_settings(
    payload: ClientSettingsPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    require_permission(context, "settings.manage")
    values = payload.model_dump(exclude_none=True)
    if values.get("analysis_advance_minutes") not in {None, 5, 10, 15, 30}:
        raise HTTPException(status_code=422, detail="Invalid analysis advance")
    if values.get("history_window_days") not in {None, 3, 7, 14, 30}:
        raise HTTPException(status_code=422, detail="Invalid history window")
    if "enabled_days" in values and (
        not values["enabled_days"] or not set(values["enabled_days"]) <= set(range(7))
    ):
        raise HTTPException(status_code=422, detail="Invalid enabled days")
    report_threshold = values.get("signal_report_threshold")
    problem_threshold = values.get("signal_problem_threshold")
    immediate_threshold = values.get("signal_immediate_threshold")
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    report_threshold = (
        report_threshold if report_threshold is not None else settings.signal_report_threshold
    )
    problem_threshold = (
        problem_threshold if problem_threshold is not None else settings.signal_problem_threshold
    )
    immediate_threshold = (
        immediate_threshold
        if immediate_threshold is not None
        else settings.signal_immediate_threshold
    )
    if not report_threshold <= problem_threshold <= immediate_threshold:
        raise HTTPException(status_code=422, detail="Signal thresholds must be ordered")
    for key, value in values.items():
        setattr(settings, key, value)
    schedule = await TenantClientRepository(session, context.tenant.id).schedule()
    if schedule:
        mapping = {
            "daily_report_time": "report_time",
            "history_window_days": "history_window_days",
            "analysis_advance_minutes": "advance_minutes",
            "analysis_enabled": "analysis_enabled",
            "enabled_days": "enabled_days",
            "timezone": "timezone",
        }
        for key, value in values.items():
            if key in mapping:
                setattr(schedule, mapping[key], value)
        schedule.next_analysis_at = next_analysis_time(
            now=datetime.now(UTC),
            timezone=schedule.timezone,
            report_time=schedule.report_time,
            enabled_days=list(schedule.enabled_days),
            advance_minutes=schedule.advance_minutes,
        )
    await session.commit()
    return await get_client_settings(context, session)


@router.get("/access")
async def access(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    schedule = await TenantClientRepository(session, context.tenant.id).schedule()
    return {
        "status": schedule.access_status if schedule else context.tenant.status,
        "started_at": schedule.access_started_at if schedule else context.tenant.created_at,
        "expires_at": schedule.access_expires_at
        if schedule
        else context.tenant.subscription_expires_at,
        "grace_period_until": schedule.grace_period_until if schedule else None,
        "analysis_enabled": schedule.analysis_enabled if schedule else False,
    }


@router.get("/menu")
async def menu(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    repository = TenantClientRepository(session, context.tenant.id)
    connection = await repository.current_connection()
    schedule = await repository.schedule()
    reports_available = len(await repository.reports())
    state = onboarding_state(connection)
    sections = {
        "ready": [
            "summary",
            "signals",
            "problems",
            "commitments",
            "dialogs",
            "employees",
            "group_integrations",
            "reports",
            "connections",
            "ai_usage",
            "settings",
        ],
        "synchronization": ["progress", "discovered_data", "cancel"],
    }.get(state, ["connect", "how_it_works", "security"])
    return {
        "sections": sections,
        "permissions": ["*"] if context.membership.role == "owner" else sorted(context.permissions),
        "features": {
            "reports": True,
            "employees": True,
            "scheduled_analysis": True,
            "incremental_ingestion": True,
            "signals": True,
            "commitments": True,
            "group_reminders": True,
            "ai_budgets": True,
        },
        "onboarding_state": state,
        "connection_state": connection.status if connection else "disconnected",
        "access_state": schedule.access_status if schedule else context.tenant.status,
        "available_reports": reports_available,
        "tenant": {"id": context.tenant.id, "name": context.tenant.name},
    }
