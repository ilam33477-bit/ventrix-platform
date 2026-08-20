from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from ..models import OperationalProblem, TelegramMessage

ACK_ONLY_RE = re.compile(
    r"^\s*(?:ок|хорошо|понял[аи]?|принято|спасибо|сейчас|секунду|вижу)[.!\s]*$",
    re.IGNORECASE,
)
COMPLETION_RE = re.compile(
    r"\b(?:отправил[аи]?|готово|прикрепил[аи]?|сделано|выполнено|оплатил[аи]?|"
    r"исправил[аи]?|решено|закрыли|договор во вложении|сч[её]т во вложении)\b",
    re.IGNORECASE,
)
NEGATIVE_RE = re.compile(
    r"\b(?:не готово|не сделал[аи]?|не отправил[аи]?|не получается|позже|переносим)\b",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


class RemediationAIProvider(Protocol):
    async def generate_json(self, **kwargs): ...


class _AIResult(BaseModel):
    outcome: Literal["fixed", "not_fixed", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)


@dataclass(frozen=True, slots=True)
class RemediationDecision:
    outcome: Literal["fixed", "not_fixed", "uncertain"]
    confidence: float
    reason: str
    method: str
    evidence_message_ids: tuple[str, ...]


class RemediationVerifier:
    version = "remediation-v1"

    def __init__(
        self,
        provider: RemediationAIProvider | None = None,
        *,
        model: str = "deepseek-v4-flash",
        auto_close_confidence: float = 0.82,
    ) -> None:
        self.provider = provider
        self.model = model
        self.auto_close_confidence = auto_close_confidence

    async def verify(
        self,
        problem: OperationalProblem,
        messages: list[TelegramMessage],
    ) -> RemediationDecision:
        deterministic = self._deterministic(problem, messages)
        if deterministic.outcome != "uncertain" or self.provider is None:
            return deterministic
        payload = {
            "problem": {
                "type": problem.problem_type,
                "evidence": problem.evidence,
                "expected_action": problem.recommended_action,
            },
            "later_messages": [
                {
                    "id": item.id,
                    "outgoing": item.outgoing,
                    "text": (item.body_text or "")[:1500],
                    "has_attachments": bool(item.attachments_json),
                }
                for item in messages[-8:]
            ],
        }
        try:
            raw, _usage = await self.provider.generate_json(
                model=self.model,
                system_prompt=(
                    "Verify whether later Telegram messages prove the expected remediation. "
                    "Return JSON only: outcome fixed|not_fixed|uncertain, confidence 0..1, reason. "
                    "Acknowledgement alone is not proof. Prefer uncertain when evidence is weak. "
                    "Write the user-facing reason in Russian; keep names and quoted text unchanged."
                ),
                payload=payload,
                max_tokens=350,
            )
            parsed = _AIResult.model_validate(json.loads(raw))
        except (ValueError, TypeError, ValidationError, json.JSONDecodeError):
            return deterministic
        outcome = parsed.outcome
        if outcome == "fixed" and parsed.confidence < self.auto_close_confidence:
            outcome = "uncertain"
        return RemediationDecision(
            outcome,
            parsed.confidence,
            (
                parsed.reason
                if CYRILLIC_RE.search(parsed.reason)
                else "Новые сообщения проверены, но результат требует подтверждения."
            ),
            "ai",
            tuple(item.id for item in messages[-8:]),
        )

    def _deterministic(
        self,
        problem: OperationalProblem,
        messages: list[TelegramMessage],
    ) -> RemediationDecision:
        outgoing = [item for item in messages if item.outgoing and (item.body_text or "").strip()]
        if not outgoing:
            return RemediationDecision(
                "not_fixed", 0.98, "Нет нового ответа сотрудника.", "rule", ()
            )
        evidence_ids = tuple(item.id for item in outgoing)
        texts = [(item.body_text or "").strip() for item in outgoing]
        if all(ACK_ONLY_RE.fullmatch(text) for text in texts):
            return RemediationDecision(
                "not_fixed",
                0.96,
                "Есть только подтверждение получения, но нет доказательства выполнения.",
                "rule",
                evidence_ids,
            )
        if any(NEGATIVE_RE.search(text) for text in texts):
            return RemediationDecision(
                "not_fixed",
                0.9,
                "Новое сообщение явно сообщает о невыполнении.",
                "rule",
                evidence_ids,
            )
        has_attachment = any(item.attachments_json for item in outgoing)
        if any(COMPLETION_RE.search(text) for text in texts) and (
            has_attachment or max(map(len, texts)) >= 12
        ):
            return RemediationDecision(
                "fixed",
                0.92,
                "Есть явное подтверждение выполненного действия.",
                "rule",
                evidence_ids,
            )
        if problem.problem_type in {
            "waiting_customer",
            "client_without_answer",
            "customer_question",
        }:
            source_tokens = self._meaningful_tokens(problem.evidence)
            response_tokens = self._meaningful_tokens(" ".join(texts))
            overlap = len(source_tokens & response_tokens)
            if max(map(len, texts)) >= 30 and (overlap >= 1 or "?" not in problem.evidence):
                return RemediationDecision(
                    "fixed",
                    0.84,
                    "Сотрудник дал содержательный ответ по контексту проблемы.",
                    "rule",
                    evidence_ids,
                )
        return RemediationDecision(
            "uncertain",
            0.55,
            "Новые сообщения есть, но они не доказывают выполнение ожидаемого действия.",
            "rule",
            evidence_ids,
        )

    @staticmethod
    def _meaningful_tokens(value: str) -> set[str]:
        return {token.lower() for token in TOKEN_RE.findall(value) if len(token) >= 5}
