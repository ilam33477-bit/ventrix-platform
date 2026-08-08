from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock_time
from typing import Annotated, Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings, get_settings
from ..database import get_session
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
    OperationalProblem,
    Permission,
    ProductEvent,
    Report,
    ReportMetric,
    ReportProblem,
    ReportSection,
    Signal,
    TelegramConnection,
    TelegramDialog,
    Tenant,
    TenantMembership,
    TenantSettings,
)
from ..repositories.client_data import TenantClientRepository
from ..scheduler.service import TenantAnalysisScheduler, next_analysis_time
from ..services.encryption import EncryptionService
from ..telegram_sessions.gateway import TelethonGateway
from ..telegram_sessions.service import TelegramConnectionError, TelegramConnectionService
from ..timezones import normalize_timezone

router = APIRouter(prefix="/api/v1/client", tags=["tenant-client"])


class ProblemPatch(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|in_progress|resolved|false_positive)$")


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
    ai_daily_soft_limit: int | None = Field(default=None, ge=1)
    ai_daily_hard_limit: int | None = Field(default=None, ge=1)
    employee_notifications_enabled: bool | None = None
    group_reminders_enabled: bool | None = None

    @field_validator("timezone")
    @classmethod
    def normalize_settings_timezone(cls, value: str | None) -> str | None:
        return normalize_timezone(value) if value is not None else None


class EmployeeCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    telegram_user_id: int | None = Field(default=None, gt=0)
    telegram_username: str | None = Field(default=None, max_length=64)
    role: str = Field(default="employee", min_length=1, max_length=64)
    notifications_enabled: bool = True
    criticality_threshold: int = Field(default=85, ge=0, le=100)


class GroupIntegrationCreate(BaseModel):
    telegram_chat_id: int
    title: str = Field(min_length=1, max_length=300)
    notifications_enabled: bool = True
    minimum_criticality: int = Field(default=85, ge=0, le=100)
    reminder_cooldown_minutes: int = Field(default=120, ge=1, le=10_080)


class TelegramLoginStart(BaseModel):
    phone: str = Field(min_length=8, max_length=24)
    employee_id: str | None = None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = "+" + "".join(character for character in value if character.isdigit())
        if len(normalized) < 9:
            raise ValueError("Invalid phone number")
        return normalized


class TelegramLoginComplete(BaseModel):
    code: SecretStr | None = None
    password: SecretStr | None = None


class TelegramConnectionScope(BaseModel):
    folder_ids: list[int] = Field(min_length=1, max_length=20)
    history_days: int = Field(default=7)
    personal_dialogs_consent: bool = False


def get_client_connection_service(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> TelegramConnectionService:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram user-session integration is not configured",
        )
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    return TelegramConnectionService(
        factory,
        EncryptionService(settings.app_encryption_key.get_secret_value()),
        TelethonGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
        ),
    )


def require_permission(context: ClientAuthContext, permission: str) -> None:
    if not context.allows(permission):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")


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
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    )
    critical_threshold = settings.signal_immediate_threshold if settings else 85
    counts: dict[str, Any] = {
        "problems": int(
            await session.scalar(
                select(func.count(OperationalProblem.id)).where(
                    OperationalProblem.tenant_id == tenant_id,
                    OperationalProblem.status.not_in(("resolved", "false_positive")),
                )
            )
            or 0
        ),
        "signals": int(
            await session.scalar(
                select(func.count(Signal.id)).where(
                    Signal.tenant_id == tenant_id,
                    Signal.criticality >= critical_threshold,
                )
            )
            or 0
        ),
        "commitments": int(
            await session.scalar(
                select(func.count(Commitment.id)).where(
                    Commitment.tenant_id == tenant_id,
                    Commitment.status == "open",
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
    input_tokens, output_tokens, calls = (
        await session.execute(
            select(
                func.coalesce(func.sum(AIUsageCall.input_tokens), 0),
                func.coalesce(func.sum(AIUsageCall.output_tokens), 0),
                func.count(AIUsageCall.id),
            ).where(
                AIUsageCall.tenant_id == tenant_id,
                AIUsageCall.occurred_at >= day_start,
            )
        )
    ).one()
    counts["ai_usage"] = {
        "tokens_today": int(input_tokens or 0) + int(output_tokens or 0),
        "calls_today": int(calls or 0),
    }
    return counts


@router.post("/mini-app/auth")
async def mini_app_auth(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    settings = await session.scalar(
        select(TenantSettings).where(TenantSettings.tenant_id == context.tenant.id)
    )
    connection = await TenantClientRepository(session, context.tenant.id).current_connection()
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
        },
        "dashboard_summary": await mini_app_dashboard_summary(session, context),
    }


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
    if connection.selected_folder_id is None:
        return "folder_selection"
    if connection.progress_stage == "scope_selected":
        return "chat_selection"
    return "connecting"


@router.get("/bootstrap")
async def client_bootstrap(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    tenant = context.tenant
    connections = list(
        await session.scalars(
            select(TelegramConnection)
            .where(
                TelegramConnection.tenant_id == tenant.id,
                TelegramConnection.deleted_at.is_(None),
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
    problems = list(
        await session.scalars(
            select(OperationalProblem)
            .where(
                OperationalProblem.tenant_id == tenant.id,
                OperationalProblem.status == "open",
            )
            .order_by(OperationalProblem.occurred_at.desc())
            .limit(20)
        )
    )
    state = onboarding_state(connection)
    menus = {
        "not_connected": ["Подключить Telegram", "Как это работает", "Безопасность"],
        "connecting": ["Подключение", "Как это работает", "Безопасность"],
        "folder_selection": ["Выбор папки", "Подключения", "Безопасность"],
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
        "tenant": {"id": tenant.id, "name": tenant.name},
        "role": context.membership.role,
        "permissions": sorted(context.permissions),
        "onboarding_state": state,
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
        "problems": [
            {
                "id": problem.id,
                "type": problem.problem_type,
                "priority": problem.priority,
                "confidence": problem.confidence,
                "evidence": problem.evidence,
                "explanation": problem.explanation,
                "recommended_action": problem.recommended_action,
                "occurred_at": problem.occurred_at,
            }
            for problem in problems
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
                    OperationalProblem.status != "resolved",
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
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Problem not found")
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
        "occurred_at": item.occurred_at,
    }


@router.patch("/problems/{problem_id}")
async def update_problem(
    problem_id: str,
    payload: ProblemPatch,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    require_permission(context, "problems.manage")
    item = await TenantClientRepository(session, context.tenant.id).problem(problem_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Problem not found")
    item.status = payload.status
    event = "problem_false_positive" if payload.status == "false_positive" else "problem_resolved"
    await record_event(session, context, event, {"problem_id": item.id})
    await session.commit()
    return {"id": item.id, "status": item.status}


@router.get("/reports")
async def reports(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    rows = await TenantClientRepository(session, context.tenant.id).reports()
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
        for item in rows
    ]


@router.get("/reports/{report_id}")
async def report_detail(
    report_id: str,
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
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
            )
            .order_by(TelegramConnection.created_at.desc())
        )
    )
    return [
        {
            "id": item.id,
            "status": item.status,
            "account": item.display_name or item.phone_masked,
            "username": item.username,
            "health_status": item.health_status,
            "last_health_check_at": item.last_health_check_at,
            "last_sync_at": item.last_sync_at,
            "folder": item.selected_folder_title,
        }
        for item in rows
    ]


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
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram login could not be started") from exc
    return {
        "id": connection.id,
        "status": connection.status,
        "phone_masked": connection.phone_masked,
        "employee_id": employee.id if employee else None,
    }


@router.post("/connections/{connection_id}/login/complete")
async def complete_connection_login(
    connection_id: str,
    payload: TelegramLoginComplete,
    context: ClientContext,
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
    except TelegramConnectionError as exc:
        raise HTTPException(status_code=409, detail="Login session is no longer available") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Telegram rejected the code or 2FA password"
        ) from exc
    finally:
        code = password = None
    return {
        "id": connection.id,
        "status": connection.status,
        "account": connection.display_name or connection.phone_masked,
        "username": connection.username,
        "requires_2fa": connection.status == "awaiting_2fa",
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
        }
        for item in rows
    ]


@router.get("/employees")
async def employees(
    context: ClientContext,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(Employee)
            .where(Employee.tenant_id == context.tenant.id)
            .order_by(Employee.display_name)
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
        }
        for item in rows
    ]


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
    await session.commit()
    return {"id": employee.id, "name": employee.display_name}


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
