from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..models import TelegramMessage
from ..timezones import normalize_timezone

LOW_VALUE_RE = re.compile(
    r"^(?:ок|okay|спасибо|благодарю|понял[аи]?|принято|👍|👌|✅|🙏)[.! ]*$", re.IGNORECASE
)
PRICE_RE = re.compile(r"\b(?:стоимост|цен[ауы]|прайс|тариф|сколько стоит|бюджет)\w*", re.IGNORECASE)
CONTRACT_RE = re.compile(
    r"\b(?:договор|контракт|оферт|кп|коммерческ\w+ предложен)\w*", re.IGNORECASE
)
PAYMENT_RE = re.compile(r"\b(?:сч[её]т|invoice|оплат|реквизит|ндс|предоплат)\w*", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"\b\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:₽|руб(?:лей|ля|\.)?|usd|eur|\$|€)\b",
    re.IGNORECASE,
)
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
    r"\b(?:сегодня|завтра|послезавтра|через\s+(?:один|два|три|четыре|пять|\d+)\s+д(?:ень|ня|ней)|"
    r"до\s+(?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья)|"
    r"в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)|"
    r"на\s+следующей\s+неделе|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|"
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b(?:(?:(?:до|к)\s*)(?:[01]?\d|2[0-3])[:.]\d{2}|(?:[01]?\d|2[0-3]):\d{2})\b",
    re.IGNORECASE,
)

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среду": 2,
    "среды": 2,
    "четверг": 3,
    "четверга": 3,
    "пятницу": 4,
    "пятницы": 4,
    "субботу": 5,
    "субботы": 5,
    "воскресенье": 6,
    "воскресенья": 6,
}
NUMBER_WORDS = {"один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5}


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
        timezone: str = "Europe/Moscow",
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
            message.sender_role in {"customer", "external"}
            and last
            and not last.outgoing
            and self._normalized(last.body_text) == self._normalized(text)
        )
        external_sender = message.sender_role in {"customer", "external"} or (
            message.sender_role in {None, "unknown"} and not message.outgoing
        )
        unanswered_before = bool(external_sender and last and not last.outgoing)
        attachment_names = [
            str(item.get("name") or "").lower() for item in message.attachments_json
        ]
        attachment_mimes = {
            str(item.get("mime_type") or "").lower() for item in message.attachments_json
        }
        invoice_filename = any(
            any(marker in name for marker in ("invoice", "счет", "счёт", "payment", "оплат"))
            for name in attachment_names
        )
        invoice_document = invoice_filename and (
            any(name.endswith((".pdf", ".doc", ".docx", ".xlsx")) for name in attachment_names)
            or "application/pdf" in attachment_mimes
        )
        features = {
            "question": "?" in text,
            "external_sender": external_sender,
            "silence_before_hours": round(silence_hours, 2),
            "repeated_message": repeated,
            "unanswered_before": unanswered_before,
            "price": bool(PRICE_RE.search(text)),
            "contract": bool(CONTRACT_RE.search(text)),
            "payment": bool(PAYMENT_RE.search(text)),
            "amount": bool(AMOUNT_RE.search(text)),
            "invoice_filename": invoice_filename,
            "invoice_document": invoice_document,
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
        common = (15 if features["question"] else 0) + (10 if external_sender else 0)
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

        invoice_confidence = 0
        if features["invoice_document"]:
            invoice_confidence += 58
        if features["payment"]:
            invoice_confidence += 24
        if features["amount"]:
            invoice_confidence += 18
        if invoice_confidence >= 58:
            candidates.append(
                LocalSignalCandidate(
                    "invoice_received",
                    min(98, invoice_confidence),
                    "получен вероятный счёт или платёжный документ",
                    dict(features),
                )
            )

        if features["promise"] and message.sender_role in {"account_owner", "employee"}:
            deadline = parse_deadline(text, self._aware(message.sent_at), timezone)
            candidates.append(
                LocalSignalCandidate(
                    "employee_commitment",
                    min(88, 45 + (12 if deadline else 0) + (8 if features["document"] else 0)),
                    "сотрудник сформулировал обещание",
                    dict(features),
                    deadline,
                )
            )
        if external_sender and (repeated or unanswered_before):
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
        if features["question"] and external_sender and not candidates:
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


def parse_deadline(
    text: str,
    sent_at: datetime,
    timezone: str = "Europe/Moscow",
) -> datetime | None:
    """Parse supported Russian business deadlines in the tenant timezone."""
    zone = ZoneInfo(normalize_timezone(timezone))
    local_sent = LocalSignalEngine._aware(sent_at).astimezone(zone)
    lowered = " ".join(text.lower().split())
    time_match = TIME_RE.search(lowered)
    date_match = DATE_RE.search(lowered)
    if time_match is None and date_match is None:
        return None
    hour, minute = 18, 0
    if time_match:
        clock_match = re.search(r"(\d{1,2})[:.](\d{2})", time_match.group())
        if clock_match:
            hour, minute = int(clock_match.group(1)), int(clock_match.group(2))

    target = local_sent.date()
    explicit_date = False
    if "послезавтра" in lowered:
        target += timedelta(days=2)
    elif "завтра" in lowered:
        target += timedelta(days=1)
    elif "сегодня" in lowered:
        pass
    elif match := re.search(r"через\s+(один|два|три|четыре|пять|\d+)\s+д", lowered):
        raw = match.group(1)
        target += timedelta(days=NUMBER_WORDS.get(raw, int(raw) if raw.isdigit() else 0))
    elif "на следующей неделе" in lowered:
        target += timedelta(days=(7 - target.weekday()))
    elif match := re.search(
        r"(?:до\s+|в\s+)(понедельника|понедельник|вторника|вторник|среды|среду|"
        r"четверга|четверг|пятницы|пятницу|субботы|субботу|воскресенья|воскресенье)",
        lowered,
    ):
        wanted = WEEKDAYS[match.group(1)]
        days = (wanted - target.weekday()) % 7
        candidate = datetime.combine(target, time(hour, minute), zone)
        if days == 0 and candidate <= local_sent:
            days = 7
        target += timedelta(days=days)
    elif match := re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", lowered):
        day, month = int(match.group(1)), int(match.group(2))
        year_text = match.group(3)
        year = local_sent.year if year_text is None else int(year_text)
        if year < 100:
            year += 2000
        target = date(year, month, day)
        explicit_date = True
        if year_text is None and datetime.combine(target, time(hour, minute), zone) <= local_sent:
            target = date(year + 1, month, day)
    elif match := re.search(
        r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)(?:\s+(\d{4}))?\b",
        lowered,
    ):
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        year_text = match.group(3)
        year = int(year_text) if year_text else local_sent.year
        target = date(year, month, day)
        explicit_date = True
        if year_text is None and datetime.combine(target, time(hour, minute), zone) <= local_sent:
            target = date(year + 1, month, day)

    deadline = datetime.combine(target, time(hour, minute), zone)
    if (
        not explicit_date
        and date_match is None
        and deadline <= local_sent
        or "сегодня" in lowered
        and deadline <= local_sent
    ):
        deadline += timedelta(days=1)
    return deadline
