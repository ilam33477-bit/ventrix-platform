from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .message_relevance import classify_message_relevance, dialogue_is_explicitly_closed

ConversationStateName = Literal[
    "WAITING_FOR_EMPLOYEE",
    "WAITING_FOR_CLIENT",
    "CLOSED_SUCCESS",
    "CLOSED_REJECTED",
    "CLOSED_NEUTRAL",
    "ACTIVE_SUPPORT",
    "ACTIVE_SALES",
    "FOLLOWUP_LATER",
    "AMBIGUOUS",
]

TERMINAL_RE = re.compile(
    r"^(?!.*\?)(?:(?:вс[её][, ]+)?(?:хорошо|ок(?:ей)?|понял[аи]?|ясно)[,!. )]*)?"
    r"(?:(?:спасибо|благодарю)(?:\s+(?:большое|вам|за\s+(?:ответ|помощь|предложение|информацию)))?"
    r"|буду\s+иметь\s+в\s+виду|посмотрю|потестирую|буду\s+тестировать|да[, ]+взаимно|"
    r"доброго\s+дня|вс[её]\s+(?:хорошо|понятно))?[!.) 🙏👍👌]*$",
    re.IGNORECASE,
)
REFUSAL_RE = re.compile(
    r"(?:\bне\s+(?:интересно|актуально|нужно|хочу|подходит|готов[аы]?)\b|"
    r"\bне\s+особо\s+горю\s+желанием\b|\bчерез\s+ботов\b.{0,80}\bне\s+работаю\b|"
    r"\bсистема\s+не\s+интересует\b|\bнет[, ]+спасибо\b|\bоткажусь\b)",
    re.IGNORECASE,
)
QUESTION_OR_REQUEST_RE = re.compile(
    r"(?:\?|\b(?:как|какое|какая|какие|сколько|куда|когда|почему|зачем|можно|пришлите|"
    r"отправьте|просит|расскажите|объясните|давайте\s+попроб|хочу\s+попроб|есть\s+новости|"
    r"ну\s+что|ау|не\s+открывается|не\s+работает|не\s+помогло)\b)",
    re.IGNORECASE,
)
TECHNICAL_RE = re.compile(
    r"(?:не\s+(?:открывается|работает|грузится|запускается|помогло)|ошибк|просто\s+загрузка|"
    r"завис|сломал|кнопк\w*\s+не)",
    re.IGNORECASE,
)
PAYMENT_RE = re.compile(
    r"(?:сколько\s+сто|цен[ауы]|тариф|оплат|сч[её]т|invoice|подписк\w*\s+сто)", re.IGNORECASE
)
PARTNERSHIP_RE = re.compile(
    r"(?:клиентск\w+\s+баз|привожу\s+пользовател|фикс\s+или\s+процент|"
    r"делим\s+доход|партн[её]рств|сотрудничеств)",
    re.IGNORECASE,
)
PRODUCT_MISMATCH_RE = re.compile(
    r"(?:не\s+(?:совсем\s+)?подходит|мне\s+нужн\w+.{0,80}(?:а\s+тут|но)|"
    r"ожидал[аи]?\s+другое|нет\s+нужн\w+\s+функц|"
    r"не\s+устраивает|ужасн|обман|(?:хочу|требую)\s+возврат|плохо\s+работает)",
    re.IGNORECASE,
)
INTEREST_RE = re.compile(
    r"(?:\bинтересно\b|стало\s+интересно|давайте\s+попроб|хочу\s+попроб|"
    r"пришлите\s+ссылк|расскажите\s+подробнее)",
    re.IGNORECASE,
)
FILLER_RE = re.compile(r"^(?:а|э|мм+|\.\.\.|[!?]{1,2})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConversationAssessment:
    conversation_state: ConversationStateName
    response_required: bool
    action_required: bool
    issue_family: str | None
    severity: str | None
    confidence: float
    client_intent: str
    last_meaningful_client_message: str | None
    reason: str
    next_action: str | None
    evidence_message_ids: tuple[str | int, ...]
    close_existing_issue_families: tuple[str, ...] = ()
    followup_at: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "conversation_state": self.conversation_state,
            "response_required": self.response_required,
            "action_required": self.action_required,
            "issue_family": self.issue_family,
            "severity": self.severity,
            "confidence": self.confidence,
            "client_intent": self.client_intent,
            "last_meaningful_client_message": self.last_meaningful_client_message,
            "reason": self.reason,
            "next_action": self.next_action,
            "evidence_message_ids": list(self.evidence_message_ids),
            "close_existing_issue_families": list(self.close_existing_issue_families),
            "followup_at": self.followup_at,
        }


def is_terminal_message(text: str | None) -> bool:
    normalized = " ".join((text or "").split())
    if not normalized or "?" in normalized:
        return False
    if TERMINAL_RE.fullmatch(normalized):
        return True
    lowered = re.sub(r"^[\s.,!)(🙏🏻👍🏻👌]+|[\s.,!)(🙏🏻👍🏻👌]+$", "", normalized.casefold())
    # Common multi-clause acknowledgements are terminal only when they contain no
    # request, problem or unresolved action. This deliberately stays conservative.
    has_thanks = "спасиб" in lowered or "благодар" in lowered
    acknowledges = any(
        marker in lowered
        for marker in ("все, хорошо", "всё, хорошо", "понял", "потестирую", "иметь в виду")
    )
    short_thanks = has_thanks and len(lowered) <= 140
    return bool(
        (short_thanks or (has_thanks and acknowledges))
        and not QUESTION_OR_REQUEST_RE.search(lowered)
        and not TECHNICAL_RE.search(lowered)
        and not PAYMENT_RE.search(lowered)
    )


def _message(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "id": item.get("id") or item.get("telegram_message_id"),
            "outgoing": bool(item.get("outgoing")),
            "text": " ".join(str(item.get("text") or item.get("body_text") or "").split()),
        }
    return {
        "id": getattr(item, "telegram_message_id", None) or getattr(item, "id", None),
        "outgoing": bool(getattr(item, "outgoing", False)),
        "text": " ".join(str(getattr(item, "body_text", "") or "").split()),
    }


def assess_conversation(messages: list[Any]) -> ConversationAssessment:
    normalized = [_message(item) for item in messages[-20:]]
    normalized = [item for item in normalized if item["text"]]
    client_messages = [
        item
        for item in normalized
        if not item["outgoing"]
        and not FILLER_RE.fullmatch(item["text"])
        and classify_message_relevance(item["text"]).message_class not in {"service", "advertising"}
    ]
    last_client = client_messages[-1] if client_messages else None
    last = normalized[-1] if normalized else None
    client_text = last_client["text"] if last_client else None
    evidence = (last_client["id"],) if last_client and last_client["id"] is not None else ()

    if dialogue_is_explicitly_closed(normalized):
        return ConversationAssessment(
            "CLOSED_REJECTED",
            False,
            False,
            None,
            None,
            0.98,
            "REJECTION_ACCEPTED",
            client_text,
            "Клиент отказался, сотрудник принял отказ, диалог завершён.",
            None,
            evidence,
            ("UNANSWERED_REQUEST", "COMMERCIAL_OPPORTUNITY", "FOLLOWUP"),
        )
    if client_text and REFUSAL_RE.search(client_text):
        return ConversationAssessment(
            "CLOSED_REJECTED",
            False,
            False,
            None,
            None,
            0.95,
            "REJECTION",
            client_text,
            "Клиент явно отказался; обязательного действия сотрудника нет.",
            None,
            evidence,
            ("UNANSWERED_REQUEST", "COMMERCIAL_OPPORTUNITY", "FOLLOWUP"),
        )
    if last and not last["outgoing"] and is_terminal_message(last["text"]):
        return ConversationAssessment(
            "CLOSED_SUCCESS",
            False,
            False,
            None,
            None,
            0.97,
            "ACKNOWLEDGEMENT",
            client_text,
            "Клиент подтвердил получение информации и естественно завершил разговор.",
            None,
            evidence,
            ("UNANSWERED_REQUEST",),
        )
    if last and last["outgoing"]:
        return ConversationAssessment(
            "WAITING_FOR_CLIENT",
            False,
            False,
            "FOLLOWUP" if "?" in last["text"] else None,
            None,
            0.92,
            "EMPLOYEE_SENT_NEXT_STEP",
            client_text,
            "Последний содержательный шаг сделал сотрудник; сейчас ожидается клиент.",
            None,
            evidence,
            ("UNANSWERED_REQUEST",),
        )
    if not last_client:
        return ConversationAssessment(
            "AMBIGUOUS",
            False,
            False,
            None,
            None,
            0.9,
            "NONE",
            None,
            "Нет содержательного запроса клиента.",
            None,
            (),
        )

    if TECHNICAL_RE.search(client_text or ""):
        family, state, intent, action = (
            "TECHNICAL_PROBLEM",
            "ACTIVE_SUPPORT",
            "REPORTING_TECHNICAL_PROBLEM",
            "Проверить техническую проблему и дать клиенту конкретное решение.",
        )
    elif PARTNERSHIP_RE.search(client_text or ""):
        family, state, intent, action = (
            "COMMERCIAL_OPPORTUNITY",
            "ACTIVE_SALES",
            "PARTNERSHIP_PROPOSAL",
            "Ответить на предложение партнёрства и обсудить модель сотрудничества.",
        )
    elif PRODUCT_MISMATCH_RE.search(client_text or ""):
        family, state, intent, action = (
            "PRODUCT_DISSATISFACTION",
            "ACTIVE_SALES",
            "PRODUCT_MISMATCH",
            "Ответить на обратную связь и уточнить, можно ли предложить подходящий сценарий.",
        )
    elif PAYMENT_RE.search(client_text or ""):
        family, state, intent, action = (
            "PAYMENT_QUESTION",
            "WAITING_FOR_EMPLOYEE",
            "ASKING_PRICE_OR_PAYMENT",
            "Ответить на вопрос о цене или оплате.",
        )
    elif QUESTION_OR_REQUEST_RE.search(client_text or ""):
        reengaged = bool(INTEREST_RE.search(client_text or ""))
        family, state, intent, action = (
            "COMMERCIAL_OPPORTUNITY" if reengaged else "UNANSWERED_REQUEST",
            "ACTIVE_SALES" if reengaged else "WAITING_FOR_EMPLOYEE",
            "REENGAGED_LEAD" if reengaged else "ASKING_DETAILS",
            "Ответить на последний содержательный вопрос клиента.",
        )
    elif INTEREST_RE.search(client_text or ""):
        family, state, intent, action = (
            "COMMERCIAL_OPPORTUNITY",
            "ACTIVE_SALES",
            "CONFIRMED_INTEREST",
            "Продолжить коммерческий диалог и предложить следующий шаг.",
        )
    else:
        return ConversationAssessment(
            "AMBIGUOUS",
            False,
            False,
            None,
            None,
            0.86,
            "UNCLEAR",
            client_text,
            "Нет подтверждённого запроса или незавершённого действия сотрудника.",
            None,
            evidence,
        )

    return ConversationAssessment(
        state,  # type: ignore[arg-type]
        True,
        True,
        family,
        "HIGH",
        0.9,
        intent,
        client_text,
        "Последнее содержательное сообщение клиента требует ответа сотрудника.",
        action,
        evidence,
    )
