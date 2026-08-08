from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..analysis.service import JSONAIProvider
from ..database import SQLiteTransactionManager
from ..jobs.queue import JobDeferred, JobLease, SQLiteJobQueue
from ..models import (
    AIUsageCall,
    AIUsageMetric,
    Commitment,
    DialogState,
    OperationalProblem,
    Signal,
    TelegramDialog,
    TelegramMessage,
    TenantSettings,
)
from .notifications import NotificationOrchestrator
from .triage import TriageResult, parse_triage_result

TRIAGE_SYSTEM_PROMPT = """You triage one new Telegram business event. Return JSON only.
Required keys: criticality (0-100), category, requires_immediate_attention,
requires_employee_notification, requires_manager_notification, reason,
recommended_action, recommended_deadline_minutes (integer or null), needs_deep_analysis.
Do not invent facts or message IDs. Evaluate context, not keywords alone.
"""


class AITriageService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: SQLiteJobQueue,
        provider: JSONAIProvider | None,
        *,
        model: str,
        context_limit: int = 8,
        notification_cooldown_minutes: int = 120,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.provider = provider
        self.model = model
        self.context_limit = context_limit
        self.transactions = SQLiteTransactionManager(session_factory)
        self.notifications = NotificationOrchestrator(
            session_factory, queue, cooldown_minutes=notification_cooldown_minutes
        )

    async def triage(self, job: JobLease) -> dict[str, object]:
        if self.provider is None:
            raise RuntimeError("AI provider is not configured")
        signal_id = str(job.payload["signal_id"])
        payload, signal, settings = await self._payload(signal_id, job.tenant_id)
        if signal.status in {"triaged", "problem_created", "history"}:
            return {"signal_id": signal.id, "status": signal.status, "deduplicated": True}
        await self._enforce_budget(signal, settings)
        started = time.perf_counter()
        raw = ""
        usage: dict[str, int] = {}
        call_started = time.perf_counter()
        try:
            raw, usage = await self.provider.generate_json(
                model=self.model,
                system_prompt=TRIAGE_SYSTEM_PROMPT,
                payload=payload,
                max_tokens=900,
            )
            try:
                result, repaired = parse_triage_result(raw)
            except ValidationError:
                await self._record_usage(
                    job,
                    signal,
                    usage,
                    int((time.perf_counter() - call_started) * 1000),
                    "invalid_json",
                    "ValidationError",
                )
                call_started = time.perf_counter()
                raw, usage = await self.provider.generate_json(
                    model=self.model,
                    system_prompt=TRIAGE_SYSTEM_PROMPT
                    + "\nPrevious response was invalid. Return complete valid JSON only.",
                    payload=payload,
                    max_tokens=900,
                )
                result, repaired = parse_triage_result(raw)
        except Exception as exc:
            await self._record_usage(
                job,
                signal,
                usage,
                int((time.perf_counter() - started) * 1000),
                "failed",
                type(exc).__name__,
            )
            raise
        await self._record_usage(
            job,
            signal,
            usage,
            int((time.perf_counter() - call_started) * 1000),
            "completed",
            None,
        )
        problem_id = await self._apply_result(signal.id, result, settings, repaired)
        notification_ids = await self.notifications.plan_for_signal(signal.id, problem_id)
        return {
            "signal_id": signal.id,
            "criticality": result.criticality,
            "problem_id": problem_id,
            "notifications": len(notification_ids),
        }

    async def _payload(
        self, signal_id: str, tenant_id: str | None
    ) -> tuple[dict[str, object], Signal, TenantSettings]:
        async with self.session_factory() as session:
            signal = await session.scalar(
                select(Signal).where(Signal.id == signal_id, Signal.tenant_id == tenant_id)
            )
            if signal is None:
                raise LookupError("signal not found in tenant")
            message = await session.get(TelegramMessage, signal.source_message_id)
            dialog = await session.get(TelegramDialog, signal.dialog_id)
            state = await session.scalar(
                select(DialogState).where(DialogState.dialog_id == signal.dialog_id)
            )
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == signal.tenant_id)
            )
            messages = list(
                await session.scalars(
                    select(TelegramMessage)
                    .where(
                        TelegramMessage.dialog_id == signal.dialog_id,
                        TelegramMessage.telegram_message_id <= message.telegram_message_id,
                    )
                    .order_by(TelegramMessage.sent_at.desc())
                    .limit(self.context_limit)
                )
            )
            messages.reverse()
            commitments = list(
                await session.scalars(
                    select(Commitment).where(
                        Commitment.dialog_id == signal.dialog_id,
                        Commitment.status == "open",
                    )
                )
            )
        payload = {
            "signal": {
                "type": signal.signal_type,
                "local_score": signal.local_score,
                "reason": signal.reason,
                "features": signal.metadata_json.get("features", {}),
            },
            "dialog": {
                "type": dialog.classification or dialog.dialog_type,
                "compact_summary": state.compact_summary if state else "",
                "unresolved_questions": state.unresolved_questions_json if state else [],
            },
            "new_message": self._message_payload(message),
            "recent_messages": [self._message_payload(item) for item in messages],
            "open_commitments": [
                {
                    "type": item.commitment_type,
                    "deadline": item.deadline_at.isoformat() if item.deadline_at else None,
                    "expected_action": item.expected_action,
                }
                for item in commitments
            ],
            "sla_minutes": settings.response_sla_minutes,
        }
        return payload, signal, settings

    async def _enforce_budget(self, signal: Signal, settings: TenantSettings) -> None:
        if settings.ai_daily_hard_limit is None:
            return
        day_start = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
        async with self.session_factory() as session:
            used = int(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(AIUsageCall.input_tokens + AIUsageCall.output_tokens), 0
                        )
                    ).where(
                        AIUsageCall.tenant_id == signal.tenant_id,
                        AIUsageCall.occurred_at >= day_start,
                    )
                )
            )
        if (
            used >= settings.ai_daily_hard_limit
            and signal.local_score < settings.signal_immediate_threshold
        ):
            raise JobDeferred(3600, "tenant_ai_daily_hard_limit")

    async def _apply_result(
        self,
        signal_id: str,
        result: TriageResult,
        settings: TenantSettings,
        repaired: bool,
    ) -> str | None:
        async def write(session: AsyncSession) -> str | None:
            signal = await session.get(Signal, signal_id)
            signal.ai_score = result.criticality
            signal.criticality = result.criticality
            signal.processed_at = datetime.now(UTC)
            signal.reason = result.reason
            signal.metadata_json = {
                **signal.metadata_json,
                "triage": result.model_dump(mode="json"),
                "json_repaired": repaired,
            }
            state = await session.scalar(
                select(DialogState).where(DialogState.dialog_id == signal.dialog_id)
            )
            if state:
                source = await session.get(TelegramMessage, signal.source_message_id)
                state.last_ai_processed_message_id = source.telegram_message_id
            if result.criticality < settings.signal_report_threshold:
                signal.status = "history"
                return None
            signal.status = "triaged"
            if result.criticality < settings.signal_problem_threshold:
                return None
            problem = await session.scalar(
                select(OperationalProblem).where(OperationalProblem.signal_id == signal.id)
            )
            if problem is None:
                source = await session.get(TelegramMessage, signal.source_message_id)
                problem = OperationalProblem(
                    tenant_id=signal.tenant_id,
                    connection_id=signal.telegram_connection_id,
                    dialog_id=signal.dialog_id,
                    source_message_id=signal.source_message_id,
                    signal_id=signal.id,
                    fingerprint=f"signal:{signal.fingerprint}",
                    problem_type=result.category,
                    priority=self._priority(result.criticality, settings),
                    confidence=result.criticality / 100,
                    evidence=(source.body_text or "")[:2000],
                    explanation=result.reason,
                    recommended_action=result.recommended_action,
                    occurred_at=source.sent_at,
                )
                session.add(problem)
                await session.flush()
            signal.status = "problem_created"
            return problem.id

        return await self.transactions.run(write)

    async def _record_usage(
        self,
        job: JobLease,
        signal: Signal,
        usage: dict[str, int],
        duration_ms: int,
        status: str,
        error_code: str | None,
    ) -> None:
        async def write(session: AsyncSession) -> None:
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            session.add(
                AIUsageCall(
                    tenant_id=signal.tenant_id,
                    job_id=job.id,
                    signal_id=signal.id,
                    model=self.model,
                    job_type=job.job_type,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    status=status,
                    error_code=error_code,
                )
            )
            day = datetime.now(UTC).date()
            metric = await session.scalar(
                select(AIUsageMetric).where(
                    AIUsageMetric.tenant_id == signal.tenant_id,
                    AIUsageMetric.metric_date == day,
                    AIUsageMetric.model == self.model,
                )
            )
            if metric is None:
                metric = AIUsageMetric(
                    tenant_id=signal.tenant_id,
                    metric_date=day,
                    model=self.model,
                )
                session.add(metric)
            metric.input_tokens = (metric.input_tokens or 0) + input_tokens
            metric.output_tokens = (metric.output_tokens or 0) + output_tokens
            metric.request_count = (metric.request_count or 0) + 1

        await self.transactions.run(write)

    @staticmethod
    def _message_payload(message: TelegramMessage) -> dict[str, object]:
        return {
            "id": message.telegram_message_id,
            "sent_at": message.sent_at.isoformat(),
            "outgoing": message.outgoing,
            "text": message.body_text,
            "attachments": message.attachments_json,
        }

    @staticmethod
    def _priority(criticality: int, settings: TenantSettings) -> str:
        if criticality >= settings.signal_immediate_threshold:
            return "critical"
        if criticality >= settings.signal_problem_threshold:
            return "high"
        if criticality >= settings.signal_report_threshold:
            return "medium"
        return "low"
