from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.ai_router import RouteName

from ..database import SQLiteTransactionManager
from ..intelligence.conversation_state import assess_conversation
from ..intelligence.message_relevance import dialogue_is_explicitly_closed
from ..intelligence.problem_lifecycle import initialize_problem_lifecycle
from ..jobs.queue import JobDeferred, JobLease, SQLiteJobQueue
from ..models import (
    AIUsageCall,
    AIUsageMetric,
    AnalysisBatch,
    AnalysisRun,
    Commitment,
    DialogState,
    Employee,
    GroupIntegration,
    OperationalProblem,
    Report,
    ReportGenerationRun,
    ReportMetric,
    ReportProblem,
    ReportSection,
    Signal,
    TelegramConnection,
    TelegramDialog,
    TelegramMessage,
    TenantAnalysisSchedule,
    TenantDailyMetric,
    TenantSettings,
)
from ..reporting.business_metrics import build_employee_business_performance
from ..services.encryption import EncryptionService
from ..services.product_events import add_system_event
from ..telegram_sessions.service import TelegramConnectionService
from .budget import ConservativeTokenEstimator, ModelInputBudget
from .preprocessing import AnalysisBatchBuilder
from .schema import AnalysisResponse, parse_analysis_response


class JSONAIProvider(Protocol):
    async def generate_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: dict[str, Any],
        thinking: bool = False,
        reasoning_effort: str | None = None,
        max_tokens: int = 4000,
    ) -> tuple[str, dict[str, int]]: ...


SYSTEM_PROMPT = """Analyze each Telegram business dialog in the supplied dialogs array independently. Return json only.
Use schema_version 1.0 and exactly this structure:
{"schema_version":"1.0","tenant_id":"...","batch_id":"...","dialog_results":[{"chat_id":"...","dialog_type":"...","summary":"...","participants":[],"detected_patterns":[],"problems":[{"event_type":"...","is_problem":true,"priority":"medium","confidence":0.8,"requires_review":false,"source_message_ids":[],"evidence":[],"summary":"...","recommended_action":"..."}],"business_outcomes":[{"outcome_type":"call_scheduled","explicitly_supported":true,"confidence":0.9,"source_message_ids":[],"summary":"...","amount":null,"currency":null}]}],"usage":{"input_tokens":0,"output_tokens":0}}.
Never invent source message IDs or facts. Use the tenant profile and local features supplied.
Never transfer facts, participants, message IDs, evidence, or conclusions between dialogs. Return one dialog_result for every supplied dialog id.
Use only these event_type values: client_without_answer, customer_complaint,
customer_question, commitment_risk, overdue_commitment, payment_risk, deal_risk,
churn_risk, conflict, task_risk, operational_risk.
Set is_problem=true only for a concrete actionable business risk supported by the
source messages. Ordinary conversation, acknowledgements and neutral questions
without a missed action are not problems.
The goal is to find missed opportunities, unanswered actionable messages and open
discussion threads — not every dialog whose last message is from a customer.
Before emitting client_without_answer, identify the exact latest unanswered question,
request, agreed follow-up, payment/document action or other open line of discussion.
If a customer declined, said they were not interested, or rejected the offer and the
employee accepted that outcome (for example "понял, без проблем", "не буду настаивать",
"если передумаете, я на связи"), the conversation is completed. Courtesy replies such
as "хорошо, спасибо за предложение" do not reopen it. Emit no problem for that dialog.
If the evidence can also be explained as a completed polite sales conversation, prefer
is_problem=false. Silence alone is never sufficient evidence of a problem.
For every candidate also return conversation_state, response_required, action_required,
issue_family, evidence_message_ids and close_existing_issue_families. EMPLOYEE/outgoing is
the monitored employee; CLIENT/incoming is external. Never infer client interest from an
employee pitch. Prefer NO_ACTION when uncertain. A main problem requires literal evidence,
action_required=true, and confidence >=0.80 for high/critical or >=0.85 for medium.
All user-facing summary and recommended_action values must be written in Russian.
Do not translate names, usernames or verbatim evidence. Keep JSON keys and enum values
in the required English schema. Emit client_without_answer only when supplied data
explicitly proves that the configured response deadline has already passed.
Extract business_outcomes only from literal evidence in the same dialog. A call is
scheduled only when its time/date or explicit agreement is confirmed. A sale is
confirmed only when purchase, payment or order acceptance is explicit; never treat a
pitch, price discussion or client interest as a sale. Set explicitly_supported=false
when uncertain. Include exact source_message_ids. amount and currency are allowed only
when the confirmed sale explicitly states them. Do not estimate revenue or working hours.
"""


CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def russian_user_text(value: str, fallback: str) -> str:
    """Keep client-visible generated copy Russian if a provider ignores the prompt."""
    normalized = value.strip()
    cyrillic_count = len(CYRILLIC_RE.findall(normalized))
    latin_count = len(re.findall(r"[A-Za-z]", normalized))
    return normalized if cyrillic_count >= max(4, latin_count) else fallback


CANONICAL_PROBLEM_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "client_without_answer",
        ("without_answer", "no_response", "unanswered", "late_reply"),
    ),
    ("customer_complaint", ("complaint", "dissatisfaction", "negative_feedback", "жалоб")),
    ("customer_question", ("customer_question", "unresolved_question", "вопрос")),
    ("overdue_commitment", ("overdue", "missed_deadline", "broken_promise", "просроч")),
    ("commitment_risk", ("commitment", "promise", "deadline_risk", "обещан")),
    ("payment_risk", ("payment", "invoice", "refund", "оплат", "счет", "счёт")),
    ("deal_risk", ("deal", "contract", "lost_lead", "сделк", "договор")),
    ("churn_risk", ("churn", "lost_customer", "client_loss", "уход")),
    ("conflict", ("conflict", "escalation", "конфликт")),
    ("task_risk", ("task", "follow_up", "followup", "задач")),
)
CANONICAL_PROBLEM_TYPES = frozenset(
    {item[0] for item in CANONICAL_PROBLEM_HINTS} | {"operational_risk"}
)


def canonical_problem_type(value: str) -> str:
    normalized = re.sub(r"[^a-zа-я0-9]+", "_", value.strip().lower()).strip("_")
    if normalized in CANONICAL_PROBLEM_TYPES:
        return normalized
    for canonical, hints in CANONICAL_PROBLEM_HINTS:
        if any(hint in normalized for hint in hints):
            return canonical
    return "operational_risk"


def canonical_issue_family(problem_type: str) -> str:
    value = problem_type.casefold()
    if any(marker in value for marker in ("payment", "invoice", "price", "оплат")):
        return "PAYMENT_QUESTION"
    if any(marker in value for marker in ("technical", "support", "error", "технич")):
        return "TECHNICAL_PROBLEM"
    if any(marker in value for marker in ("complaint", "dissatisfaction", "mismatch")):
        return "PRODUCT_DISSATISFACTION"
    if any(marker in value for marker in ("lead", "commercial", "partnership", "opportunity")):
        return "COMMERCIAL_OPPORTUNITY"
    if any(marker in value for marker in ("commitment", "promise", "deadline")):
        return "PROMISE_DEADLINE"
    if "follow" in value or "task" in value:
        return "FOLLOWUP"
    return "UNANSWERED_REQUEST" if "answer" in value or "question" in value else "OTHER"


class AnalysisPipelineService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        *,
        connection_service: TelegramConnectionService | None = None,
        provider: JSONAIProvider | None = None,
        queue: SQLiteJobQueue | None = None,
        token_budget: int = 50_000,
        fast_model: str = "deepseek-v4-flash",
        deep_model: str = "deepseek-v4-pro",
        model_budget: ModelInputBudget | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.connection_service = connection_service
        self.provider = provider
        self.queue = queue or SQLiteJobQueue(session_factory)
        self.transactions = SQLiteTransactionManager(session_factory)
        self.model_budget = model_budget or ModelInputBudget()
        self.estimator = ConservativeTokenEstimator()
        self.builder = AnalysisBatchBuilder(
            session_factory,
            fast_model=fast_model,
            deep_model=deep_model,
            model_budget=self.model_budget,
            system_prompt=SYSTEM_PROMPT,
        )
        self.token_budget = token_budget

    async def pipeline(self, job: JobLease) -> dict[str, Any]:
        if job.tenant_id is None:
            raise ValueError("analysis pipeline requires tenant")
        if job.telegram_account_id is None:
            connections = await self._connections(job.tenant_id)
            tenant_run = await self._get_or_create_tenant_run(job, connections)
            for connection in connections:
                await self.queue.enqueue(
                    "analysis.connection",
                    {**job.payload, "tenant_run_id": tenant_run.id},
                    tenant_id=job.tenant_id,
                    telegram_account_id=connection.id,
                    priority=job.attempts + 40,
                    idempotency_key=f"analysis-connection:{job.id}:{connection.id}",
                    correlation_id=job.correlation_id or job.id,
                    is_heavy=True,
                    category="analysis",
                    cost_class="heavy",
                    max_attempts=5,
                )
            await self.queue.enqueue(
                "analysis.aggregate",
                {"tenant_run_id": tenant_run.id},
                tenant_id=job.tenant_id,
                priority=80,
                idempotency_key=f"analysis-aggregate:{tenant_run.id}",
                correlation_id=tenant_run.correlation_id,
                is_heavy=True,
                category="report",
                cost_class="heavy",
                max_attempts=5,
            )
            return {
                "tenant_run_id": tenant_run.id,
                "connections": len(connections),
                "fan_out": True,
            }
        connection = await self._connection(job.tenant_id, job.telegram_account_id)
        if connection is None or connection.session_secret_id is None:
            raise RuntimeError("tenant Telegram connection is unavailable")
        run = await self._get_or_create_run(job, connection.id)
        batch_ids = await self.builder.build(
            run.id,
            history_window_days=int(job.payload.get("history_window_days", 30)),
            dialog_ids={job.dialog_id}
            if job.job_type == "analysis.deep" and job.dialog_id
            else None,
        )
        for batch_id in batch_ids:
            await self.queue.enqueue(
                "ai_batch_analysis",
                {"batch_id": batch_id},
                tenant_id=job.tenant_id,
                telegram_account_id=connection.id,
                priority=60,
                idempotency_key=f"ai-batch:{batch_id}",
                correlation_id=run.correlation_id,
                is_heavy=True,
                category="ai_heavy",
                cost_class="heavy",
                max_attempts=3,
            )
        if not job.payload.get("tenant_run_id") and job.job_type != "analysis.deep":
            await self.queue.enqueue(
                "report_generation",
                {"analysis_run_id": run.id},
                tenant_id=job.tenant_id,
                telegram_account_id=connection.id,
                priority=80,
                idempotency_key=f"report-generation:{run.id}",
                correlation_id=run.correlation_id,
                is_heavy=True,
                category="report",
                cost_class="heavy",
                max_attempts=3,
            )
        if job.job_type == "analysis.deep" and not batch_ids:
            await self._finish_deep_run(run.id)
        return {
            "analysis_run_id": run.id,
            "batches": len(batch_ids),
            "report_suppressed": job.job_type == "analysis.deep",
        }

    async def process_batch(self, job: JobLease) -> dict[str, Any]:
        try:
            return await self._process_batch(job)
        except Exception:
            if job.attempts + 1 >= job.max_attempts:
                await self._mark_batch_failed(str(job.payload.get("batch_id")), "ai_batch_failed")
            raise

    async def _process_batch(self, job: JobLease) -> dict[str, Any]:
        if self.provider is None:
            raise RuntimeError("AI provider is not configured")
        batch_id = str(job.payload["batch_id"])
        async with self.session_factory() as session:
            batch = await session.scalar(
                select(AnalysisBatch).where(
                    AnalysisBatch.id == batch_id,
                    AnalysisBatch.tenant_id == job.tenant_id,
                )
            )
            if batch is None:
                raise LookupError("analysis batch not found in tenant")
            run = await session.get(AnalysisRun, batch.run_id)
            if run.input_tokens + run.output_tokens >= run.token_budget:
                raise RuntimeError("tenant report token budget exhausted")
            payload = dict(batch.payload_json)
            payload["tenant_id"] = batch.tenant_id
            payload["batch_id"] = batch.id
            model = batch.model or "deepseek-v4-flash"
            deep = batch.route_name in {RouteName.DEEP.value, RouteName.CRITICAL.value}
            input_budget = self.model_budget.usable_input_tokens(self.estimator.text(SYSTEM_PROMPT))
            actual_estimate = self.estimator.payload(payload)
            if actual_estimate > input_budget:
                raise RuntimeError("AI batch exceeds configured model input budget")
        call_started = time.perf_counter()
        raw, usage = await self.provider.generate_json(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            thinking=deep,
            reasoning_effort="high" if deep else None,
            max_tokens=self.model_budget.max_output_tokens,
        )
        repaired = False
        try:
            parsed, repaired = parse_analysis_response(raw)
            self._validate_identity(parsed, batch)
        except Exception:  # noqa: BLE001 - one controlled retry for invalid provider JSON
            raw, usage = await self.provider.generate_json(
                model=model,
                system_prompt=SYSTEM_PROMPT
                + "\nPrevious output was invalid. Return complete valid json.",
                payload=payload,
                thinking=deep,
                reasoning_effort="high" if deep else None,
                max_tokens=self.model_budget.max_output_tokens,
            )
            parsed, repaired = parse_analysis_response(raw)
            self._validate_identity(parsed, batch)
        await self._store_batch_result(
            batch_id,
            parsed,
            raw,
            usage,
            repaired,
            job_id=job.id,
            duration_ms=int((time.perf_counter() - call_started) * 1000),
        )
        problems = await self._create_problems(batch_id, parsed)
        await self._finish_deep_run(run.id)
        return {"batch_id": batch_id, "problems": problems}

    async def _finish_deep_run(self, run_id: str, *, force: bool = False) -> None:
        async def write(session: AsyncSession) -> None:
            run = await session.get(AnalysisRun, run_id)
            if run is None or run.trigger != "signal_escalation" or run.status == "completed":
                return
            remaining = int(
                await session.scalar(
                    select(func.count(AnalysisBatch.id)).where(
                        AnalysisBatch.run_id == run.id,
                        AnalysisBatch.status != "completed",
                    )
                )
                or 0
            )
            if remaining and not force:
                return
            run.status = "completed"
            run.stage = "deep_analysis_completed"
            run.finished_at = datetime.now(UTC)

        await self.transactions.run(write)

    async def generate_report(self, job: JobLease) -> dict[str, Any]:
        run_id = str(job.payload["analysis_run_id"])
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.id == run_id,
                    AnalysisRun.tenant_id == job.tenant_id,
                )
            )
            batches = list(
                await session.scalars(select(AnalysisBatch).where(AnalysisBatch.run_id == run_id))
            )
        if run is None:
            raise RuntimeError("analysis run not found")
        if run.trigger == "signal_escalation":
            await self._finish_deep_run(run.id, force=True)
            return {
                "analysis_run_id": run.id,
                "report_id": None,
                "report_suppressed": True,
            }
        if any(item.status in {"pending", "running"} for item in batches):
            raise JobDeferred(2, "waiting_for_required_ai_batches")
        await self._record_batch_outcome(run_id, batches)
        result = await self._build_report(run_id)
        if not result.get("report_id"):
            return result
        report_id = str(result["report_id"])
        await self.queue.enqueue(
            "report_delivery",
            {"report_id": report_id},
            tenant_id=job.tenant_id,
            priority=90,
            idempotency_key=f"report-delivery:{report_id}",
            correlation_id=job.id,
            is_heavy=False,
            category="report",
        )
        await self.queue.enqueue(
            "statistics_refresh",
            {"report_id": report_id},
            tenant_id=job.tenant_id,
            priority=100,
            idempotency_key=f"statistics-refresh:{report_id}",
            correlation_id=job.id,
            is_heavy=False,
            category="general",
        )
        return result

    async def aggregate_tenant_run(self, job: JobLease) -> dict[str, Any]:
        tenant_run_id = str(job.payload["tenant_run_id"])
        async with self.session_factory() as session:
            tenant_run = await session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.id == tenant_run_id,
                    AnalysisRun.tenant_id == job.tenant_id,
                )
            )
            if tenant_run is None:
                raise LookupError("tenant analysis run not found")
            expected_connection_ids = set(
                (tenant_run.metrics_json or {}).get("expected_connection_ids", [])
            )
            children = list(
                await session.scalars(
                    select(AnalysisRun).where(
                        AnalysisRun.tenant_id == job.tenant_id,
                        AnalysisRun.telegram_account_id.in_(expected_connection_ids),
                    )
                )
            )
            children = [
                child
                for child in children
                if (child.metrics_json or {}).get("tenant_run_id") == tenant_run_id
            ]
            child_ids = [child.id for child in children]
            batches = (
                list(
                    await session.scalars(
                        select(AnalysisBatch).where(AnalysisBatch.run_id.in_(child_ids))
                    )
                )
                if child_ids
                else []
            )
        if len(children) < len(expected_connection_ids):
            raise JobDeferred(2, "waiting_for_account_analysis_runs")
        if any(item.status in {"pending", "running"} for item in batches):
            raise JobDeferred(2, "waiting_for_account_ai_batches")

        async def finish_children(session: AsyncSession) -> None:
            current = await session.get(AnalysisRun, tenant_run_id)
            current.required_batches = len(batches)
            current.completed_batches = sum(item.status == "completed" for item in batches)
            current.failed_batches = sum(item.status == "failed" for item in batches)
            current.input_tokens = sum(item.input_tokens or 0 for item in batches)
            current.output_tokens = sum(item.output_tokens or 0 for item in batches)
            current.metrics_json = {
                **(current.metrics_json or {}),
                "processed_dialog_versions": self._processed_dialog_versions(
                    [item for item in batches if item.status == "completed"]
                ),
                "analysis_partial": current.failed_batches > 0,
            }
            if current.failed_batches:
                current.delayed_reason = "partial_ai_batch_failure"
            for child in children:
                stored = await session.get(AnalysisRun, child.id)
                stored.status = "completed"
                stored.stage = "aggregated_partial" if stored.failed_batches else "aggregated"
                stored.finished_at = datetime.now(UTC)

        await self.transactions.run(finish_children)
        result = await self._build_report(tenant_run_id)
        if not result.get("report_id"):
            return {**result, "connections": len(children), "consolidated": True}
        report_id = str(result["report_id"])
        await self.queue.enqueue(
            "report_delivery",
            {"report_id": report_id},
            tenant_id=job.tenant_id,
            priority=90,
            idempotency_key=f"report-delivery:{report_id}",
            correlation_id=tenant_run_id,
            category="report",
        )
        await self.queue.enqueue(
            "statistics_refresh",
            {"report_id": report_id},
            tenant_id=job.tenant_id,
            priority=100,
            idempotency_key=f"statistics-refresh:{report_id}",
            correlation_id=tenant_run_id,
            category="general",
        )
        return {**result, "connections": len(children), "consolidated": True}

    async def _connection(self, tenant_id: str, connection_id: str) -> TelegramConnection | None:
        async with self.session_factory() as session:
            return await session.scalar(
                select(TelegramConnection)
                .where(
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.id == connection_id,
                    TelegramConnection.deleted_at.is_(None),
                )
                .limit(1)
            )

    async def _connections(self, tenant_id: str) -> list[TelegramConnection]:
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                        TelegramConnection.session_secret_id.is_not(None),
                        TelegramConnection.status.in_(("connected", "ready")),
                    )
                )
            )

    async def _get_or_create_run(self, job: JobLease, connection_id: str) -> AnalysisRun:
        async def write(session: AsyncSession) -> str:
            existing = await session.scalar(
                select(AnalysisRun).where(AnalysisRun.correlation_id == job.id)
            )
            if existing:
                return existing.id
            due = job.payload.get("report_due_at")
            run = AnalysisRun(
                tenant_id=job.tenant_id,
                telegram_account_id=connection_id,
                trigger=str(job.payload.get("trigger") or "manual"),
                status="running",
                stage="message_preprocessing",
                report_due_at=datetime.fromisoformat(due) if due else None,
                started_at=datetime.now(UTC),
                token_budget=self.token_budget,
                metrics_json={
                    "history_window_days": int(job.payload.get("history_window_days", 30)),
                    "tenant_run_id": job.payload.get("tenant_run_id"),
                },
                correlation_id=job.id,
            )
            session.add(run)
            await session.flush()
            await add_system_event(
                session,
                tenant_id=run.tenant_id,
                event_name="scheduled_analysis_started"
                if run.trigger == "scheduled"
                else "manual_analysis_started",
                metadata={"analysis_run_id": run.id},
            )
            return run.id

        run_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            return await session.get(AnalysisRun, run_id)

    async def _get_or_create_tenant_run(
        self, job: JobLease, connections: list[TelegramConnection]
    ) -> AnalysisRun:
        correlation_id = job.correlation_id or job.id

        async def write(session: AsyncSession) -> str:
            existing = await session.scalar(
                select(AnalysisRun).where(AnalysisRun.correlation_id == correlation_id)
            )
            if existing:
                return existing.id
            due = job.payload.get("report_due_at")
            run = AnalysisRun(
                tenant_id=job.tenant_id,
                telegram_account_id=None,
                trigger=str(job.payload.get("trigger") or "manual"),
                status="running",
                stage="account_fan_out",
                report_due_at=datetime.fromisoformat(due) if due else None,
                started_at=datetime.now(UTC),
                token_budget=self.token_budget,
                metrics_json={
                    "history_window_days": int(job.payload.get("history_window_days", 30)),
                    "expected_connection_ids": [item.id for item in connections],
                },
                correlation_id=correlation_id,
            )
            session.add(run)
            await session.flush()
            return run.id

        run_id = await self.transactions.run(write)
        async with self.session_factory() as session:
            return await session.get(AnalysisRun, run_id)

    @staticmethod
    def _validate_identity(parsed: AnalysisResponse, batch: AnalysisBatch) -> None:
        if parsed.tenant_id != batch.tenant_id or parsed.batch_id != batch.id:
            raise ValueError("AI response identity mismatch")
        expected = {str(item["id"]) for item in (batch.payload_json or {}).get("dialogs", [])}
        returned = {str(item.chat_id) for item in parsed.dialog_results}
        if returned != expected:
            raise ValueError("AI response dialog identity mismatch")

    async def _store_batch_result(
        self,
        batch_id: str,
        parsed: AnalysisResponse,
        raw: str,
        usage: dict[str, int],
        repaired: bool,
        *,
        job_id: str,
        duration_ms: int,
    ) -> None:
        async def write(session: AsyncSession) -> None:
            batch = await session.get(AnalysisBatch, batch_id)
            batch.status = "completed"
            batch.result_json = parsed.model_dump(mode="json")
            batch.raw_response_ciphertext = self.encryption.encrypt(raw)
            batch.input_tokens = usage.get("input_tokens", 0)
            batch.output_tokens = usage.get("output_tokens", 0)
            batch.repair_attempted = repaired
            session.add(
                AIUsageCall(
                    tenant_id=batch.tenant_id,
                    job_id=job_id,
                    model=batch.model or "unknown",
                    job_type="ai_batch_analysis",
                    input_tokens=batch.input_tokens,
                    output_tokens=batch.output_tokens,
                    duration_ms=duration_ms,
                    status="completed",
                )
            )
            run = await session.get(AnalysisRun, batch.run_id)
            run.completed_batches += 1
            run.input_tokens += batch.input_tokens
            run.output_tokens += batch.output_tokens
            metric = await session.scalar(
                select(AIUsageMetric).where(
                    AIUsageMetric.tenant_id == batch.tenant_id,
                    AIUsageMetric.metric_date == datetime.now(UTC).date(),
                    AIUsageMetric.model == batch.model,
                )
            )
            if metric is None:
                metric = AIUsageMetric(
                    tenant_id=batch.tenant_id,
                    analysis_run_id=run.id,
                    metric_date=datetime.now(UTC).date(),
                    model=batch.model or "unknown",
                )
                session.add(metric)
            metric.input_tokens += batch.input_tokens
            metric.output_tokens += batch.output_tokens
            metric.request_count += 1

        await self.transactions.run(write)

    async def _create_problems(self, batch_id: str, parsed: AnalysisResponse) -> int:
        async with self.session_factory() as session:
            batch = await session.get(AnalysisBatch, batch_id)
            dialog_ids = {str(item["id"]) for item in (batch.payload_json or {}).get("dialogs", [])}
            dialogs = {
                item.id: item
                for item in await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.tenant_id == batch.tenant_id,
                        TelegramDialog.id.in_(dialog_ids),
                    )
                )
            }
        created = 0

        async def write(session: AsyncSession) -> None:
            nonlocal created
            for result in parsed.dialog_results:
                dialog = dialogs.get(str(result.chat_id))
                if dialog is None:
                    continue
                connection = await session.get(TelegramConnection, dialog.connection_id)
                dialog_connection_employee = connection.assigned_employee_id if connection else None
                for candidate in result.problems:
                    if (
                        not candidate.is_problem
                        or not candidate.source_message_ids
                        or candidate.confidence < 0.5
                    ):
                        continue
                    problem_type = canonical_problem_type(candidate.event_type)
                    latest = list(
                        await session.scalars(
                            select(TelegramMessage)
                            .where(
                                TelegramMessage.tenant_id == batch.tenant_id,
                                TelegramMessage.dialog_id == dialog.id,
                                TelegramMessage.deleted_at.is_(None),
                            )
                            .order_by(TelegramMessage.telegram_message_id.desc())
                            .limit(20)
                        )
                    )
                    latest.reverse()
                    assessment = assess_conversation(latest)
                    if (
                        dialogue_is_explicitly_closed(
                            [{"outgoing": item.outgoing, "text": item.body_text} for item in latest]
                        )
                        or not candidate.action_required
                        or (
                            not assessment.action_required
                            and problem_type
                            not in {"commitment_risk", "overdue_commitment", "payment_risk"}
                        )
                    ):
                        continue
                    issue_family = candidate.issue_family or canonical_issue_family(problem_type)
                    source_remote_id = int(candidate.source_message_ids[0])
                    source = await session.scalar(
                        select(TelegramMessage).where(
                            TelegramMessage.tenant_id == batch.tenant_id,
                            TelegramMessage.dialog_id == dialog.id,
                            TelegramMessage.telegram_message_id == source_remote_id,
                        )
                    )
                    if source is None:
                        continue
                    user_summary = russian_user_text(
                        candidate.summary,
                        "Ventrix обнаружил рабочую ситуацию, требующую проверки.",
                    )
                    user_action = russian_user_text(
                        candidate.recommended_action,
                        "Проверить переписку и определить следующий шаг.",
                    )
                    signal_fingerprint = hashlib.sha256(
                        f"{batch.tenant_id}:{source.id}:{problem_type}".encode()
                    ).hexdigest()
                    signal = await session.scalar(
                        select(Signal).where(Signal.fingerprint == signal_fingerprint)
                    )
                    criticality = {
                        "critical": 95,
                        "high": 80,
                        "medium": 65,
                        "low": 40,
                        "informational": 20,
                    }.get(candidate.priority, round(candidate.confidence * 100))
                    if signal is None:
                        signal = Signal(
                            tenant_id=batch.tenant_id,
                            telegram_connection_id=dialog.connection_id,
                            dialog_id=dialog.id,
                            source_message_id=source.id,
                            employee_id=dialog_connection_employee,
                            fingerprint=signal_fingerprint,
                            signal_type=problem_type,
                            local_score=criticality,
                            ai_score=criticality,
                            criticality=criticality,
                            status="triaged",
                            reason=user_summary,
                            detected_at=source.sent_at,
                            processed_at=datetime.now(UTC),
                            metadata_json={
                                "source": "scheduled_analysis",
                                "batch_id": batch.id,
                                "evidence": candidate.evidence,
                            },
                        )
                        session.add(signal)
                        await session.flush()
                    minimum_confidence = 0.8 if candidate.priority in {"high", "critical"} else 0.85
                    if (
                        candidate.priority in {"low", "informational"}
                        or candidate.confidence < minimum_confidence
                    ):
                        signal.status = "triaged"
                        continue
                    if issue_family == "UNANSWERED_REQUEST":
                        # Scheduled/deep analysis may identify that a response is
                        # expected, but only dialog.sla_check owns promotion to an
                        # unanswered problem after the persisted deadline.
                        signal.status = "triaged"
                        continue
                    problem_fingerprint = f"signal:{signal_fingerprint}"
                    existing_problem = await session.scalar(
                        select(OperationalProblem)
                        .where(
                            OperationalProblem.tenant_id == batch.tenant_id,
                            OperationalProblem.dialog_id == dialog.id,
                            OperationalProblem.issue_family == issue_family,
                            OperationalProblem.status.in_(
                                (
                                    "new",
                                    "needs_confirmation",
                                    "acknowledged",
                                    "assigned",
                                    "in_progress",
                                    "waiting",
                                    "reopened",
                                )
                            ),
                        )
                        .order_by(OperationalProblem.occurred_at.desc())
                        .limit(1)
                    )
                    if existing_problem:
                        existing_problem.signal_id = signal.id
                        existing_problem.source_message_id = source.id
                        existing_problem.problem_type = problem_type
                        existing_problem.priority = candidate.priority
                        existing_problem.confidence = max(
                            existing_problem.confidence, candidate.confidence
                        )
                        existing_problem.evidence = "\n".join(candidate.evidence)[:4000]
                        existing_problem.explanation = user_summary
                        existing_problem.recommended_action = user_action
                        existing_problem.last_seen_at = datetime.now(UTC)
                        existing_problem.evidence_message_ids_json = list(
                            dict.fromkeys(
                                [
                                    *(existing_problem.evidence_message_ids_json or []),
                                    *candidate.evidence_message_ids,
                                    *candidate.source_message_ids,
                                ]
                            )
                        )[-20:]
                        signal.status = "problem_created"
                        continue
                    problem = OperationalProblem(
                        tenant_id=batch.tenant_id,
                        connection_id=dialog.connection_id,
                        dialog_id=dialog.id,
                        source_message_id=source.id,
                        signal_id=signal.id,
                        responsible_employee_id=dialog_connection_employee,
                        fingerprint=problem_fingerprint,
                        problem_type=problem_type,
                        issue_family=issue_family,
                        priority=candidate.priority,
                        confidence=candidate.confidence,
                        evidence="\n".join(candidate.evidence)[:4000],
                        explanation=user_summary,
                        recommended_action=user_action,
                        occurred_at=source.sent_at,
                        last_seen_at=datetime.now(UTC),
                        evidence_message_ids_json=list(
                            dict.fromkeys(
                                [*candidate.evidence_message_ids, *candidate.source_message_ids]
                            )
                        ),
                    )
                    session.add(problem)
                    await session.flush()
                    signal.status = "problem_created"
                    session.add_all(
                        initialize_problem_lifecycle(
                            problem,
                            responsible_employee_id=dialog_connection_employee,
                            requires_confirmation=candidate.requires_review,
                            reason="Ответственный определён для результата scheduled analysis.",
                        )
                    )
                    created += 1

        await self.transactions.run(write)
        return created

    async def _build_report(self, run_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            run = await session.get(AnalysisRun, run_id)
            history_window_days = int((run.metrics_json or {}).get("history_window_days", 30))
            period_start = self._aware(run.started_at) - timedelta(days=history_window_days)
            monitored_dialog_ids = select(TelegramDialog.id).where(
                TelegramDialog.tenant_id == run.tenant_id,
                TelegramDialog.selected.is_(True),
                TelegramDialog.excluded.is_(False),
            )
            problems = list(
                await session.scalars(
                    select(OperationalProblem).where(
                        OperationalProblem.tenant_id == run.tenant_id,
                        OperationalProblem.occurred_at >= period_start,
                        OperationalProblem.dialog_id.in_(monitored_dialog_ids),
                        OperationalProblem.status != "false_positive",
                    )
                )
            )
            signals = list(
                await session.scalars(
                    select(Signal).where(
                        Signal.tenant_id == run.tenant_id,
                        Signal.detected_at >= period_start,
                        Signal.dialog_id.in_(monitored_dialog_ids),
                        Signal.status != "suppressed",
                    )
                )
            )
            commitments = list(
                await session.scalars(
                    select(Commitment).where(
                        Commitment.tenant_id == run.tenant_id,
                        (Commitment.created_at >= period_start) | (Commitment.status == "open"),
                        Commitment.dialog_id.in_(monitored_dialog_ids),
                    )
                )
            )
            employees = list(
                await session.scalars(select(Employee).where(Employee.tenant_id == run.tenant_id))
            )
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.tenant_id == run.tenant_id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            )
            connections = {
                item.id: item
                for item in await session.scalars(
                    select(TelegramConnection).where(
                        TelegramConnection.tenant_id == run.tenant_id,
                        TelegramConnection.deleted_at.is_(None),
                    )
                )
            }
            groups = list(
                await session.scalars(
                    select(GroupIntegration).where(GroupIntegration.tenant_id == run.tenant_id)
                )
            )
            tenant_settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == run.tenant_id)
            )
            message_count = int(
                await session.scalar(
                    select(func.count(TelegramMessage.id)).where(
                        TelegramMessage.tenant_id == run.tenant_id,
                        TelegramMessage.dialog_id.in_(monitored_dialog_ids),
                        TelegramMessage.sent_at
                        >= run.started_at - timedelta(days=history_window_days),
                    )
                )
            )
            period_end = datetime.now(UTC)
            period_span = period_end - period_start
            previous_period_start = period_start - period_span
            current_messages = list(
                await session.scalars(
                    select(TelegramMessage).where(
                        TelegramMessage.tenant_id == run.tenant_id,
                        TelegramMessage.dialog_id.in_(monitored_dialog_ids),
                        TelegramMessage.deleted_at.is_(None),
                        TelegramMessage.sent_at >= period_start,
                        TelegramMessage.sent_at <= period_end,
                    )
                )
            )
            previous_messages = list(
                await session.scalars(
                    select(TelegramMessage).where(
                        TelegramMessage.tenant_id == run.tenant_id,
                        TelegramMessage.dialog_id.in_(monitored_dialog_ids),
                        TelegramMessage.deleted_at.is_(None),
                        TelegramMessage.sent_at >= previous_period_start,
                        TelegramMessage.sent_at < period_start,
                    )
                )
            )
        batches = await self._batches(run_id)
        dialog_outcomes: dict[str, list[dict[str, Any]]] = {}
        for batch in batches:
            for result in (batch.result_json or {}).get("dialog_results", []):
                dialog_id = str(result.get("chat_id") or "")
                dialog_outcomes.setdefault(dialog_id, []).extend(
                    list(result.get("business_outcomes") or [])
                )
        dialog_employee_ids = {
            dialog.id: (
                connections.get(dialog.connection_id).assigned_employee_id
                if connections.get(dialog.connection_id)
                else None
            )
            for dialog in dialogs
        }
        business_performance = build_employee_business_performance(
            current_messages=current_messages,
            previous_messages=previous_messages,
            dialog_employee_ids=dialog_employee_ids,
            employees=employees,
            dialog_outcomes=dialog_outcomes,
        )
        employee_report = self._employee_report(
            employees,
            signals,
            commitments,
            problems,
            tenant_settings.signal_immediate_threshold,
        )
        performance_by_employee = {
            item["employee_id"]: item for item in business_performance["rows"]
        }
        for row in employee_report["employees"]:
            row.update(performance_by_employee.get(row["employee_id"], {}))
        client_report = self._client_report(dialogs, commitments, problems)
        dialog_by_id = {item.id: item for item in dialogs}
        employee_by_id = {item.id: item for item in employees}
        important_dialogs = {
            "rows": [
                {
                    "problem_id": item.id,
                    "priority": item.priority,
                    "title": item.explanation,
                    "recommended_action": item.recommended_action,
                    "dialog_title": dialog_by_id[item.dialog_id].title
                    if item.dialog_id in dialog_by_id
                    else "Диалог",
                    "dialog_username": dialog_by_id[item.dialog_id].username
                    if item.dialog_id in dialog_by_id
                    else None,
                    "employee": employee_by_id[item.responsible_employee_id].display_name
                    if item.responsible_employee_id in employee_by_id
                    else None,
                }
                for item in sorted(
                    problems,
                    key=lambda problem: (
                        {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(problem.priority, 0),
                        problem.occurred_at,
                    ),
                    reverse=True,
                )[:20]
            ]
        }
        company_report = {
            "critical_situations": sum(
                item.criticality >= tenant_settings.signal_immediate_threshold for item in signals
            ),
            "employees": len(employees),
            "clients": sum(item.dialog_type in {"personal", "group"} for item in dialogs),
            "active_groups": sum(item.status == "active" for item in groups),
            "resolved_problems": sum(item.status == "resolved" for item in problems),
            "unresolved_problems": sum(item.status != "resolved" for item in problems),
            "open_commitments": sum(item.status == "open" for item in commitments),
        }
        metrics = {
            "messages": message_count,
            "patterns": sum(
                len((batch.result_json or {}).get("dialog_results", [])) for batch in batches
            ),
            "problems": len(problems),
            "high": sum(item.priority in {"high", "critical"} for item in problems),
            "medium": sum(item.priority == "medium" for item in problems),
            "low": sum(item.priority in {"low", "informational"} for item in problems),
            "analysis_partial": int(run.failed_batches > 0),
            "failed_analysis_batches": int(run.failed_batches),
        }

        async def write(session: AsyncSession) -> str | None:
            current = await session.get(AnalysisRun, run_id)
            report = await session.scalar(select(Report).where(Report.analysis_run_id == run_id))
            now = datetime.now(UTC)
            due_at = current.report_due_at
            if due_at is not None and due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=UTC)
            delayed = due_at is not None and now > due_at
            duplicate = await session.scalar(
                select(Report)
                .where(
                    Report.tenant_id == current.tenant_id,
                    Report.summary
                    == f"Обработано сообщений: {metrics['messages']}. Проблем: {metrics['problems']}.",
                    Report.period_end >= now - timedelta(minutes=15),
                )
                .order_by(Report.period_end.desc())
                .limit(1)
            )
            suppress_reason = None
            if report is None and duplicate is not None:
                suppress_reason = "equivalent_report_already_created"
            if report is None:
                if suppress_reason is None:
                    report = Report(
                        tenant_id=current.tenant_id,
                        analysis_run_id=current.id,
                        status="ready",
                        period_start=current.started_at - timedelta(days=history_window_days),
                        period_end=now,
                        due_at=current.report_due_at,
                        ready_at=now,
                        delivery_status="pending",
                        summary=f"Обработано сообщений: {metrics['messages']}. Проблем: {metrics['problems']}.",
                    )
                    session.add(report)
                    await session.flush()
                sections = {
                    "overview": metrics,
                    "employee_report": employee_report,
                    "client_report": client_report,
                    "company_report": company_report,
                    "business_performance": business_performance,
                    "important_dialogs": important_dialogs,
                    "recommendations": {
                        "items": [item.recommended_action for item in problems[:20]]
                    },
                }
                for position, (key, data) in enumerate(sections.items()) if report else ():
                    session.add(
                        ReportSection(
                            tenant_id=current.tenant_id,
                            report_id=report.id,
                            section_key=key,
                            position=position,
                            data_json=data,
                        )
                    )
                for key, value in metrics.items() if report else ():
                    session.add(
                        ReportMetric(
                            tenant_id=current.tenant_id,
                            report_id=report.id,
                            metric_key=key,
                            numeric_value=float(value),
                        )
                    )
                for problem in problems if report else ():
                    session.add(
                        ReportProblem(
                            tenant_id=current.tenant_id,
                            report_id=report.id,
                            problem_id=problem.id,
                        )
                    )
                if report:
                    session.add(
                        ReportGenerationRun(
                            tenant_id=current.tenant_id,
                            report_id=report.id,
                            status="completed",
                            started_at=current.started_at,
                            finished_at=now,
                            delayed_reason="completed_after_due_time" if delayed else None,
                        )
                    )
            current.status = "completed"
            current.stage = "report_ready"
            current.finished_at = now
            current.delayed_reason = (
                "completed_after_due_time"
                if delayed
                else "partial_ai_batch_failure"
                if current.failed_batches
                else None
            )
            current.metrics_json = metrics
            processed_versions = (run.metrics_json or {}).get("processed_dialog_versions", {})
            if not processed_versions:
                processed_versions = self._processed_dialog_versions(
                    list(
                        await session.scalars(
                            select(AnalysisBatch).where(AnalysisBatch.run_id == current.id)
                        )
                    )
                )
            for dialog_id, version in processed_versions.items():
                state = await session.scalar(
                    select(DialogState).where(DialogState.dialog_id == dialog_id)
                )
                if state is not None:
                    state.last_report_version = max(state.last_report_version, int(version))
            await add_system_event(
                session,
                tenant_id=current.tenant_id,
                event_name="report_ready" if report else "report_suppressed",
                metadata={
                    "report_id": report.id if report else None,
                    "analysis_run_id": current.id,
                    "reason": suppress_reason,
                },
            )
            await add_system_event(
                session,
                tenant_id=current.tenant_id,
                event_name="scheduled_analysis_delayed"
                if current.delayed_reason
                else "scheduled_analysis_completed",
                metadata={"analysis_run_id": current.id},
            )
            schedule = await session.scalar(
                select(TenantAnalysisSchedule).where(
                    TenantAnalysisSchedule.tenant_id == current.tenant_id
                )
            )
            if schedule:
                schedule.last_analysis_at = current.started_at
                schedule.last_completed_report_at = now
            daily = await session.scalar(
                select(TenantDailyMetric).where(
                    TenantDailyMetric.tenant_id == current.tenant_id,
                    TenantDailyMetric.metric_date == datetime.now(UTC).date(),
                )
            )
            if daily is None:
                session.add(
                    TenantDailyMetric(
                        tenant_id=current.tenant_id,
                        metric_date=datetime.now(UTC).date(),
                        metrics_json=metrics,
                    )
                )
            else:
                daily.metrics_json = metrics
            return report.id if report else None

        report_id = await self.transactions.run(write)
        return {
            "report_id": report_id,
            "metrics": metrics,
            "report_suppressed": report_id is None,
        }

    @staticmethod
    def _employee_report(
        employees: list[Employee],
        signals: list[Signal],
        commitments: list[Commitment],
        problems: list[OperationalProblem],
        immediate_threshold: int,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        signals_by_id = {item.id: item for item in signals}
        commitments_by_id = {item.id: item for item in commitments}
        rows = []
        for employee in employees:
            employee_signals = [item for item in signals if item.employee_id == employee.id]
            employee_commitments = [
                item for item in commitments if item.responsible_employee_id == employee.id
            ]
            employee_problems = [
                item
                for item in problems
                if item.responsible_employee_id == employee.id
                or (item.signal_id and signals_by_id.get(item.signal_id) in employee_signals)
                or (
                    item.commitment_id
                    and commitments_by_id.get(item.commitment_id) in employee_commitments
                )
            ]
            rows.append(
                {
                    "employee_id": employee.id,
                    "name": employee.display_name,
                    "critical_situations": sum(
                        item.criticality >= immediate_threshold for item in employee_signals
                    ),
                    "open_promises": sum(item.status == "open" for item in employee_commitments),
                    "missed_deadlines": sum(
                        item.status == "open"
                        and item.deadline_at is not None
                        and AnalysisPipelineService._aware(item.deadline_at) < now
                        for item in employee_commitments
                    ),
                    "resolved": sum(item.status == "resolved" for item in employee_problems),
                    "clients_waiting": sum(
                        item.status != "resolved"
                        and item.problem_type in {"waiting_customer", "client_without_answer"}
                        for item in employee_problems
                    ),
                }
            )
        return {"employees": rows}

    @staticmethod
    def _client_report(
        dialogs: list[TelegramDialog],
        commitments: list[Commitment],
        problems: list[OperationalProblem],
    ) -> dict[str, object]:
        rows = []
        for dialog in dialogs:
            dialog_commitments = [item for item in commitments if item.dialog_id == dialog.id]
            dialog_problems = [item for item in problems if item.dialog_id == dialog.id]
            if not dialog_commitments and not dialog_problems:
                continue
            rows.append(
                {
                    "dialog_id": dialog.id,
                    "title": dialog.title,
                    "open_questions": sum(
                        item.status != "resolved" and item.problem_type == "customer_question"
                        for item in dialog_problems
                    ),
                    "open_commitments": sum(item.status == "open" for item in dialog_commitments),
                    "response_delays": sum(
                        item.problem_type in {"waiting_customer", "client_without_answer"}
                        for item in dialog_problems
                    ),
                    "unresolved_problems": sum(
                        item.status != "resolved" for item in dialog_problems
                    ),
                }
            )
        return {"dialogs": rows}

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    async def _batches(self, run_id: str) -> list[AnalysisBatch]:
        async with self.session_factory() as session:
            return list(
                await session.scalars(select(AnalysisBatch).where(AnalysisBatch.run_id == run_id))
            )

    async def _record_batch_outcome(self, run_id: str, batches: list[AnalysisBatch]) -> None:
        async def write(session: AsyncSession) -> None:
            run = await session.get(AnalysisRun, run_id)
            if run is None:
                return
            run.required_batches = len(batches)
            run.completed_batches = sum(item.status == "completed" for item in batches)
            run.failed_batches = sum(item.status == "failed" for item in batches)
            run.metrics_json = {
                **(run.metrics_json or {}),
                "processed_dialog_versions": self._processed_dialog_versions(
                    [item for item in batches if item.status == "completed"]
                ),
                "analysis_partial": run.failed_batches > 0,
            }
            if run.failed_batches:
                run.delayed_reason = "partial_ai_batch_failure"

        await self.transactions.run(write)

    @staticmethod
    def _processed_dialog_versions(batches: list[AnalysisBatch]) -> dict[str, int]:
        versions: dict[str, int] = {}
        for batch in batches:
            for dialog in (batch.payload_json or {}).get("dialogs", []):
                dialog_id = str(dialog["id"])
                versions[dialog_id] = max(
                    versions.get(dialog_id, 0), int(dialog.get("state_version", 0))
                )
        return versions

    async def _fail_run(self, run_id: str, reason: str) -> None:
        async def write(session: AsyncSession) -> None:
            run = await session.get(AnalysisRun, run_id)
            run.status = "failed"
            run.stage = "failed"
            run.delayed_reason = reason
            run.finished_at = datetime.now(UTC)

        await self.transactions.run(write)

    async def _mark_batch_failed(self, batch_id: str, reason: str) -> None:
        async def write(session: AsyncSession) -> None:
            batch = await session.get(AnalysisBatch, batch_id)
            if batch is None:
                return
            batch.status = "failed"
            batch.last_error_code = reason
            run = await session.get(AnalysisRun, batch.run_id)
            run.failed_batches += 1

        await self.transactions.run(write)
