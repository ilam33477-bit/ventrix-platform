from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ops_core.ai_router import AIModelRouter, AnalysisContext, RouterPolicy

from ..database import SQLiteTransactionManager
from ..models import (
    AnalysisBatch,
    AnalysisRun,
    DialogState,
    TelegramDialog,
    TelegramMessage,
    TenantAIProfile,
    TenantSettings,
)

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
        "last_sender_outgoing": last.outgoing if last else None,
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
        if not text or text.lower().startswith(SYSTEM_EVENT_PREFIXES):
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


class AnalysisBatchBuilder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fast_model: str = "deepseek-v4-flash",
        deep_model: str = "deepseek-v4-pro",
    ) -> None:
        self.session_factory = session_factory
        self.transactions = SQLiteTransactionManager(session_factory)
        self.router = AIModelRouter(RouterPolicy(fast_model=fast_model, deep_model=deep_model))

    async def build(self, run_id: str, *, history_window_days: int = 30) -> list[str]:
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
            dialogs = list(
                await session.scalars(
                    select(TelegramDialog).where(
                        TelegramDialog.tenant_id == run.tenant_id,
                        TelegramDialog.connection_id == run.telegram_account_id,
                        TelegramDialog.selected.is_(True),
                        TelegramDialog.excluded.is_(False),
                    )
                )
            )
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

        async def write(session: AsyncSession) -> list[str]:
            batch_ids: list[str] = []
            for dialog, messages, historical_summary, state_version in prepared:
                compact = compact_messages(messages)
                if not compact:
                    continue
                features = local_features(messages)
                context_chars = sum(len(item["text"]) for item in compact)
                route = self.router.choose(
                    AnalysisContext(
                        task_type="chat_classification",
                        message_count=len(compact),
                        context_chars=context_chars,
                        participants=len(features["participants"]),
                        potential_amount=max(
                            (int(value.replace(" ", "")) for value in features["amounts"]),
                            default=None,
                        ),
                    )
                )
                batch = AnalysisBatch(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    dialog_id=dialog.id,
                    route_name=route.name.value,
                    model=route.model,
                    local_features_json=features,
                    payload_json={
                        "schema_version": "1.0",
                        "tenant": {
                            "niche": profile.niche,
                            "business_description": profile.business_description,
                            "products": profile.products,
                            "target_audience": profile.target_audience,
                            "ai_instructions": profile.additional_instructions,
                            "working_hours": settings.working_hours,
                            "response_sla_minutes": settings.response_sla_minutes,
                        },
                        "dialog": {
                            "id": dialog.id,
                            "type": dialog.classification or dialog.dialog_type,
                            "participants": features["participants"],
                            "messages": compact,
                            "historical_summary": historical_summary,
                            "local_features": features,
                            "state_version": state_version,
                        },
                    },
                )
                session.add(batch)
                await session.flush()
                batch_ids.append(batch.id)
            current = await session.get(AnalysisRun, run_id)
            current.required_batches = len(batch_ids)
            current.stage = "ai_batch_analysis"
            return batch_ids

        return await self.transactions.run(write)
