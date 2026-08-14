from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.ai_router import AIModelRouter, AnalysisContext, RouterPolicy

from ..database import SQLiteTransactionManager
from ..intelligence.message_relevance import classify_message_relevance
from ..models import (
    AnalysisBatch,
    AnalysisRun,
    DialogState,
    TelegramDialog,
    TelegramMessage,
    TenantAIFeedbackProfile,
    TenantAIProfile,
    TenantSettings,
)
from .budget import ConservativeTokenEstimator, ModelInputBudget, prompt_bytes

SYSTEM_EVENT_PREFIXES = ("joined the group", "left the group", "закрепил", "создал группу")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?<!\d)(\d[\d\s]{2,})(?:\s?(?:₽|руб|р\.|usd|eur))", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:сегодня|завтра|послезавтра|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\b", re.IGNORECASE
)
CALL_RE = re.compile(r"\b(?:созвон|встреч|звонок|zoom|meet)\w*", re.IGNORECASE)
HISTORICAL_EVIDENCE_RE = re.compile(
    r"\b(?:обеща|отправлю|пришлю|сделаю|договор|контракт|оплат|сч[её]т|жалоб|"
    r"возврат|дедлайн|срок|проблем|не получил|не ответил)\w*",
    re.IGNORECASE,
)


def local_features(messages: list[TelegramMessage], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    normalized = [item for item in messages if (item.body_text or "").strip()]
    texts = [" ".join((item.body_text or "").split()) for item in normalized]
    authors = Counter(str(item.sender_id or "unknown") for item in normalized)
    last = normalized[-1] if normalized else None
    last_at = last.sent_at if last else None
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    repeated = sum(texts[index] == texts[index - 1] for index in range(1, len(texts)))
    return {
        "message_count": len(normalized),
        "participants": sorted(authors),
        "last_message_id": last.telegram_message_id if last else None,
        "last_sender_outgoing": last.outgoing if last is not None else None,
        "minutes_without_answer": int((current - last_at).total_seconds() // 60)
        if last_at and not last.outgoing
        else 0,
        "dates": sorted({match for text in texts for match in DATE_RE.findall(text)}),
        "amounts": [match.strip() for text in texts for match in AMOUNT_RE.findall(text)],
        "links": [match for text in texts for match in URL_RE.findall(text)],
        "call_mentions": sum(bool(CALL_RE.search(text)) for text in texts),
        "repeated_messages": repeated,
        "attachment_count": sum(len(item.attachments_json) for item in normalized),
    }


def compact_messages(
    messages: list[TelegramMessage], max_chars: int = 32_000, latest_count: int = 40
) -> list[dict[str, Any]]:
    normalized: list[tuple[TelegramMessage, str]] = []
    seen: set[tuple[int | None, datetime, str]] = set()
    for item in messages:
        text = " ".join((item.body_text or "").split())
        relevance = classify_message_relevance(text)
        if (
            not text
            or text.lower().startswith(SYSTEM_EVENT_PREFIXES)
            or not relevance.business_relevant
        ):
            continue
        key = (item.sender_id, item.sent_at, text)
        if key in seen:
            continue
        seen.add(key)
        normalized.append((item, text))

    selected: list[tuple[TelegramMessage, str]] = []
    used = 0
    # Reserve the context for the newest development first. Iterating backwards
    # prevents an old busy prefix from evicting the current situation.
    for item, text in reversed(normalized[-latest_count:]):
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append((item, text[:remaining]))
        used += min(len(text), remaining)
    selected_ids = {item.telegram_message_id for item, _ in selected}
    # Fill the remaining budget with the newest historical evidence, not an
    # arbitrary prefix of the reporting window.
    historical: list[tuple[TelegramMessage, str]] = []
    for item, text in reversed(normalized[:-latest_count]):
        if item.telegram_message_id in selected_ids or not HISTORICAL_EVIDENCE_RE.search(text):
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        historical.append((item, text[:remaining]))
        used += min(len(text), remaining)
    selected.extend(historical)
    selected.sort(key=lambda pair: (pair[0].sent_at, pair[0].telegram_message_id))
    return [
        {
            "id": item.telegram_message_id,
            "sender_id": item.sender_id,
            "sent_at": item.sent_at.isoformat(),
            "outgoing": item.outgoing,
            "text": text,
            "attachments": item.attachments_json,
        }
        for item, text in selected
    ]


@dataclass(slots=True)
class PreparedDialog:
    dialog: TelegramDialog
    payload: dict[str, Any]
    features: dict[str, Any]
    route_name: str
    model: str
    estimated_tokens: int


def pack_dialog_payloads(
    dialogs: list[PreparedDialog],
    *,
    common_payload: dict[str, Any],
    budget: ModelInputBudget,
    estimator: ConservativeTokenEstimator,
    system_prompt: str,
) -> list[list[PreparedDialog]]:
    """Greedily pack complete dialog segments without crossing the input budget."""

    input_budget = budget.usable_input_tokens(estimator.text(system_prompt))
    common_tokens = estimator.payload(common_payload)
    packs: list[list[PreparedDialog]] = []
    current: list[PreparedDialog] = []
    current_tokens = common_tokens
    for item in dialogs:
        if item.estimated_tokens + common_tokens > input_budget:
            raise ValueError(f"dialog segment {item.dialog.id} exceeds model input budget")
        would_overflow = current_tokens + item.estimated_tokens > input_budget
        would_exceed_count = len(current) >= budget.max_dialogs_per_request
        if current and (would_overflow or would_exceed_count):
            packs.append(current)
            current = []
            current_tokens = common_tokens
        current.append(item)
        current_tokens += item.estimated_tokens
    if current:
        packs.append(current)
    return packs


class AnalysisBatchBuilder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fast_model: str = "deepseek-v4-flash",
        deep_model: str = "deepseek-v4-pro",
        model_budget: ModelInputBudget | None = None,
        system_prompt: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.router = AIModelRouter(RouterPolicy(fast_model=fast_model, deep_model=deep_model))
        self.model_budget = model_budget or ModelInputBudget()
        self.system_prompt = system_prompt
        self.estimator = ConservativeTokenEstimator()

    async def build(
        self,
        run_id: str,
        *,
        history_window_days: int = 30,
        dialog_ids: set[str] | None = None,
    ) -> list[str]:
        async with self.session_factory() as session:
            run = await session.get(AnalysisRun, run_id)
            if run is None:
                raise LookupError("analysis run not found")
            existing = list(
                await session.scalars(select(AnalysisBatch).where(AnalysisBatch.run_id == run_id))
            )
            if existing:
                return [item.id for item in existing]
            profile = await session.scalar(
                select(TenantAIProfile).where(TenantAIProfile.tenant_id == run.tenant_id)
            )
            settings = await session.scalar(
                select(TenantSettings).where(TenantSettings.tenant_id == run.tenant_id)
            )
            feedback_profile = await session.scalar(
                select(TenantAIFeedbackProfile).where(
                    TenantAIFeedbackProfile.tenant_id == run.tenant_id
                )
            )
            if profile is None or settings is None:
                raise LookupError("tenant analysis profile/settings not found")
            dialog_query = select(TelegramDialog).where(
                TelegramDialog.tenant_id == run.tenant_id,
                TelegramDialog.connection_id == run.telegram_account_id,
                TelegramDialog.selected.is_(True),
                TelegramDialog.excluded.is_(False),
                TelegramDialog.classification != "automated_account",
            )
            if dialog_ids is not None:
                if not dialog_ids:
                    return []
                dialog_query = dialog_query.where(TelegramDialog.id.in_(dialog_ids))
            dialogs = list(await session.scalars(dialog_query))
            incremental = run.trigger == "scheduled"
            prepared: list[tuple[TelegramDialog, list[TelegramMessage], str, int]] = []
            for dialog in dialogs:
                state = await session.scalar(
                    select(DialogState).where(DialogState.dialog_id == dialog.id)
                )
                if (
                    incremental
                    and state is not None
                    and state.meaningful_version <= state.last_report_version
                ):
                    continue
                messages = list(
                    await session.scalars(
                        select(TelegramMessage)
                        .where(
                            TelegramMessage.tenant_id == run.tenant_id,
                            TelegramMessage.dialog_id == dialog.id,
                            TelegramMessage.sent_at
                            >= datetime.now(UTC) - timedelta(days=history_window_days),
                        )
                        .order_by(TelegramMessage.sent_at.asc())
                    )
                )
                prepared.append(
                    (
                        dialog,
                        messages,
                        state.compact_summary if state else "",
                        state.meaningful_version if state else 0,
                    )
                )

        common_payload = {
            "schema_version": "1.0",
            "tenant": {
                "niche": profile.niche,
                "business_description": profile.business_description,
                "products": profile.products,
                "target_audience": profile.target_audience,
                "ai_instructions": profile.additional_instructions,
                "working_hours": settings.working_hours,
                "response_sla_minutes": settings.response_sla_minutes,
                "learned_false_positive_guidance": (
                    feedback_profile.guidance_json if feedback_profile is not None else {}
                ),
                "feedback_guidance_version": (
                    feedback_profile.version if feedback_profile is not None else 0
                ),
            },
        }
        input_budget = self.model_budget.usable_input_tokens(
            self.estimator.text(self.system_prompt)
        )
        common_tokens = self.estimator.payload(common_payload)
        # A dialog is the atomic semantic unit. Reserve room for JSON structure
        # and split only an individually oversized dialog, never by raw global message count.
        per_dialog_budget = max(1_000, input_budget - common_tokens - 256)
        dialog_segments: list[PreparedDialog] = []
        for dialog, messages, historical_summary, state_version in prepared:
            compact = compact_messages(messages, max_chars=per_dialog_budget * 2)
            if not compact:
                continue
            features = local_features(messages)
            route = self.router.choose(
                AnalysisContext(
                    task_type="chat_classification",
                    message_count=len(compact),
                    context_chars=sum(len(item["text"]) for item in compact),
                    participants=len(features["participants"]),
                    potential_amount=max(
                        (int(value.replace(" ", "")) for value in features["amounts"]),
                        default=None,
                    ),
                )
            )
            base = {
                "id": dialog.id,
                "type": dialog.classification or dialog.dialog_type,
                "participants": features["participants"],
                "historical_summary": historical_summary,
                "local_features": features,
                "state_version": state_version,
            }
            segments = self._split_dialog_messages(base, compact, per_dialog_budget)
            for segment_index, segment in enumerate(segments):
                segment["segment_index"] = segment_index
                segment["segments_total"] = len(segments)
                dialog_segments.append(
                    PreparedDialog(
                        dialog=dialog,
                        payload=segment,
                        features=features,
                        route_name=route.name.value,
                        model=route.model,
                        estimated_tokens=self.estimator.payload(segment),
                    )
                )
        packs = pack_dialog_payloads(
            dialog_segments,
            common_payload=common_payload,
            budget=self.model_budget,
            estimator=self.estimator,
            system_prompt=self.system_prompt,
        )

        async def write(session: AsyncSession) -> list[str]:
            batch_ids: list[str] = []
            for pack_index, pack in enumerate(packs):
                payload = {**common_payload, "dialogs": [item.payload for item in pack]}
                unique_dialog_ids = list(dict.fromkeys(item.dialog.id for item in pack))
                route_rank = {"fast": 0, "deep": 1, "critical": 2}
                selected_route = max(pack, key=lambda item: route_rank.get(item.route_name, 0))
                key_material = ":".join(
                    f"{item.dialog.id}:{item.payload['state_version']}:{item.payload['segment_index']}"
                    for item in pack
                )
                batch_key = (
                    f"{pack_index:04d}-{hashlib.sha256(key_material.encode()).hexdigest()[:24]}"
                )
                batch = AnalysisBatch(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    dialog_id=unique_dialog_ids[0] if len(unique_dialog_ids) == 1 else None,
                    batch_key=batch_key,
                    route_name=selected_route.route_name,
                    model=selected_route.model,
                    local_features_json={item.dialog.id: item.features for item in pack},
                    payload_json=payload,
                    estimated_input_tokens=self.estimator.payload(payload),
                    input_budget=input_budget,
                    prompt_bytes=prompt_bytes(self.system_prompt, payload),
                    dialogs_count=len(unique_dialog_ids),
                    messages_count=sum(len(item.payload["messages"]) for item in pack),
                    utilization_ratio=round(self.estimator.payload(payload) / input_budget, 4),
                )
                session.add(batch)
                await session.flush()
                batch_ids.append(batch.id)
            current = await session.get(AnalysisRun, run_id)
            if current is None:
                raise LookupError("analysis run disappeared while building batches")
            current.required_batches = len(batch_ids)
            current.stage = "ai_batch_analysis"
            return batch_ids

        return await self.transactions.run(write)

    def _split_dialog_messages(
        self,
        base: dict[str, Any],
        messages: list[dict[str, Any]],
        token_budget: int,
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        overlap: list[dict[str, Any]] = []
        for message in messages:
            candidate = {**base, "messages": [*current, message]}
            if current and self.estimator.payload(candidate) > token_budget:
                segments.append({**base, "messages": current})
                overlap = []
                overlap_tokens = 0
                for previous in reversed(current):
                    cost = self.estimator.payload(previous)
                    if overlap_tokens + cost > self.model_budget.overlap_tokens:
                        break
                    overlap.insert(0, previous)
                    overlap_tokens += cost
                current = [*overlap, message]
            else:
                current.append(message)
        if current:
            segments.append({**base, "messages": current})
        return segments
