from __future__ import annotations

import re
from dataclasses import dataclass

OTP_RE = re.compile(
    r"(?:код\s+(?:для\s+)?(?:входа|подтверждения)|login\s+code|verification\s+code|"
    r"не\s+(?:сообщайте|давайте)\s+код|пароль\s+2fa)",
    re.IGNORECASE,
)
SERVICE_RE = re.compile(
    r"(?:добро\s+пожаловать\s+в\s+групп|welcome\s+to\s+the\s+group|"
    r"заявк\w*\s+на\s+вступ|приєднатися\s+до\s+чату|пройдите\s+проверку|"
    r"подписк\w*\s+не\s+подтвержден|автоматически\s+отклонили|"
    r"joined\s+the\s+group|left\s+the\s+group)",
    re.IGNORECASE,
)
AD_MARKERS = (
    re.compile(r"\b(?:реклам|казино|ваучер|промокод|розыгрыш|акци[яи])\w*", re.IGNORECASE),
    re.compile(r"\b(?:подпишитесь|переходите|успейте|заберите|получите\s+бесплатн)\w*", re.IGNORECASE),
    re.compile(r"\b(?:бесплатн|скидк|бонус|выигра|джекпот)\w*", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class MessageRelevance:
    message_class: str
    business_relevant: bool
    reason: str


def classify_message_relevance(
    text: str | None,
    *,
    dialog_classification: str | None = None,
) -> MessageRelevance:
    """Return only high-confidence non-business classifications.

    Ambiguous sales language remains business-relevant and is left for AI triage.
    This function is intentionally conservative because suppressing a real client
    message is more harmful than sending an uncertain candidate to the model.
    """

    normalized = " ".join((text or "").split())
    if dialog_classification == "automated_account":
        return MessageRelevance("service", False, "automated Telegram account")
    if OTP_RE.search(normalized):
        return MessageRelevance("service", False, "authentication or confirmation code")
    if SERVICE_RE.search(normalized):
        return MessageRelevance("service", False, "automated group or subscription event")
    ad_score = sum(bool(pattern.search(normalized)) for pattern in AD_MARKERS)
    if ad_score >= 2:
        return MessageRelevance("advertising", False, "high-confidence promotional broadcast")
    return MessageRelevance("business", True, "potential business conversation")
