from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from ..models import TelegramMessage

LOW_VALUE_RE = re.compile(
    r"^(?:ок|okay|спасибо|благодарю|понял[аи]?|принято|👍|👌|✅|🙏)[.! ]*$", re.IGNORECASE
)
PRICE_RE = re.compile(r"\b(?:стоимост|цен[ауы]|прайс|тариф|сколько стоит|бюджет)\w*", re.IGNORECASE)
CONTRACT_RE = re.compile(
    r"\b(?:договор|контракт|оферт|кп|коммерческ\w+ предложен)\w*", re.IGNORECASE
)
PAYMENT_RE = re.compile(r"\b(?:сч[её]т|invoice|оплат|реквизит|ндс|предоплат)\w*", re.IGNORECASE)
DOCUMENT_RE = re.compile(r"\b(?:документ|акт|накладн|файл|презентац|бриф)\w*", re.IGNORECASE)
PROMISE_RE = re.compile(
    r"\b(?:отправлю|пришлю|подготовлю|перезвоню|сделаю|вернусь|уточню)\b", re.IGNORECASE
)
COMPLAINT_RE = re.compile(
    r"\b(?:жалоб|недовол|возврат|ужас|плохо|проблем|не устраива|обман)\w*", re.IGNORECASE
)
INTEREST_RE = re.compile(
    r"\b(?:готов[ыа]? начинать|хотим начать|подтвержда\w+|интересно|бер[её]м|согласны)\b",
    re.IGNORECASE,
)
NEXT_STEP_RE = re.compile(
    r"\b(?:следующ\w+ шаг|созвон|встреч|запуск|старт|подпис)\w*", re.IGNORECASE
)
DATE_RE = re.compile(
    r"\b(?:сегодня|завтра|послезавтра|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\b", re.IGNORECASE
)
TIME_RE = re.compile(r"\b(?:до\s*)?(?:[01]?\d|2[0-3])[:.]\d{2}\b")


@dataclass(frozen=True, slots=True)
class LocalSignalCandidate:
    signal_type: str
    score: int
    reason: str
    features: dict[str, Any]
    commitment_deadline: datetime | None = None


class LocalSignalEngine:
    """Cheap deterministic candidate generator. It never declares a critical incident."""

    def scan(
        self,
        message: TelegramMessage,
        previous: list[TelegramMessage],
        *,
        now: datetime | None = None,
        response_sla_minutes: int = 60,
    ) -> list[LocalSignalCandidate]:
        current = now or datetime.now(UTC)
        text = " ".join((message.body_text or "").split())
        if not text and not message.attachments_json:
            return []
        last = previous[-1] if previous else None
        last_at = self._aware(last.sent_at) if last else None
        silence_hours = (
            max(0.0, (self._aware(message.sent_at) - last_at).total_seconds() / 3600)
            if last_at
            else 0.0
        )
        repeated = bool(
            not message.outgoing
            and last
            and not last.outgoing
            and self._normalized(last.body_text) == self._normalized(text)
        )
        unanswered_before = bool(not message.outgoing and last and not last.outgoing)
        features = {
            "question": "?" in text,
            "external_sender": not message.outgoing,
            "silence_before_hours": round(silence_hours, 2),
            "repeated_message": repeated,
            "unanswered_before": unanswered_before,
            "price": bool(PRICE_RE.search(text)),
            "contract": bool(CONTRACT_RE.search(text)),
            "payment": bool(PAYMENT_RE.search(text)),
            "document": bool(DOCUMENT_RE.search(text)),
            "promise": bool(PROMISE_RE.search(text)),
            "date": bool(DATE_RE.search(text)),
            "time": bool(TIME_RE.search(text)),
            "attachment": bool(message.attachments_json),
            "complaint": bool(COMPLAINT_RE.search(text)),
            "confirmed_interest": bool(INTEREST_RE.search(text)),
            "next_step": bool(NEXT_STEP_RE.search(text)),
        }
        if LOW_VALUE_RE.fullmatch(text) and not any(
            (features["attachment"], repeated, silence_hours >= 72)
        ):
            return []

        candidates: list[LocalSignalCandidate] = []
        common = (15 if features["question"] else 0) + (10 if not message.outgoing else 0)
        if repeated:
            common += 18
        if unanswered_before:
            common += 8
        if silence_hours >= 72:
            common += 12

        rules = (
            ("commercial_question", features["price"], 42, "вопрос о стоимости или прайсе"),
            ("contract_question", features["contract"], 47, "упоминание договора или КП"),
            ("payment_question", features["payment"], 47, "оплата, счёт или реквизиты"),
            (
                "document_request",
                features["document"] or features["attachment"],
                36,
                "документ или вложение",
            ),
            ("complaint", features["complaint"], 58, "негативная обратная связь"),
            ("new_lead", features["confirmed_interest"], 54, "подтверждён коммерческий интерес"),
            ("next_step", features["next_step"], 38, "обсуждается следующий шаг"),
        )
        for signal_type, matched, base, reason in rules:
            if matched:
                candidates.append(
                    LocalSignalCandidate(
                        signal_type,
                        min(92, base + common),
                        reason,
                        dict(features),
                    )
                )

        if features["promise"] and message.outgoing:
            deadline = self._deadline(text, self._aware(message.sent_at))
            candidates.append(
                LocalSignalCandidate(
                    "employee_commitment",
                    min(88, 45 + (12 if deadline else 0) + (8 if features["document"] else 0)),
                    "сотрудник сформулировал обещание",
                    dict(features),
                    deadline,
                )
            )
        if not message.outgoing and (repeated or unanswered_before):
            score = 44 + (18 if repeated else 0)
            if last_at and (current - last_at) >= timedelta(minutes=response_sla_minutes):
                score += 12
            candidates.append(
                LocalSignalCandidate(
                    "waiting_customer",
                    min(90, score),
                    "клиент пишет повторно до ответа сотрудника",
                    dict(features),
                )
            )
        if features["question"] and not message.outgoing and not candidates:
            candidates.append(
                LocalSignalCandidate(
                    "customer_question",
                    min(55, 25 + common),
                    "новый вопрос клиента",
                    dict(features),
                )
            )
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    @staticmethod
    def _normalized(value: str | None) -> str:
        return " ".join((value or "").lower().split()).strip(".!? ")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _deadline(text: str, sent_at: datetime) -> datetime | None:
        day_offset = (
            2
            if re.search(r"\bпослезавтра\b", text, re.IGNORECASE)
            else 1
            if re.search(r"\bзавтра\b", text, re.IGNORECASE)
            else 0
        )
        time_match = TIME_RE.search(text)
        if not time_match and not DATE_RE.search(text):
            return None
        hour, minute = (18, 0)
        if time_match:
            hour, minute = map(
                int, re.sub(r"\D", ":", time_match.group()).strip(":").split(":")[-2:]
            )
        target_date = (sent_at + timedelta(days=day_offset)).date()
        deadline = datetime.combine(target_date, time(hour, minute), sent_at.tzinfo or UTC)
        if day_offset == 0 and deadline <= sent_at:
            deadline += timedelta(days=1)
        return deadline
