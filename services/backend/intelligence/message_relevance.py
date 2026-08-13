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
    re.compile(
        r"\b(?:подпишитесь|переходите|успейте|заберите|получите\s+бесплатн)\w*", re.IGNORECASE
    ),
    re.compile(r"\b(?:бесплатн|скидк|бонус|выигра|джекпот)\w*", re.IGNORECASE),
)
CLOSING_RE = re.compile(
    r"^(?:(?:да|нет|ок(?:ей)?|хорошо|понятно|понял(?:а)?|учту|взаимно|договорились|принято|ясно)[.! )🤝👍👌🙏🏻]*|"
    r"(?:спасибо|благодарю)(?:\s+(?:вам|тебе|и\s+вам|и\s+тебе|хорошо|большое))?[.! )🙏👍]*|"
    r"(?:и\s+вам|вам\s+тоже|тебе\s+тоже)(?:\s+(?:спасибо|удачи|хорошего\s+дня))?[.! )]*|"
    r"(?:удачи|хорошего\s+(?:дня|вечера)|доброго\s+дня|до\s+связи|до\s+свидания)[.! )🤝👍👌🙏🏻]*)$",
    re.IGNORECASE,
)
PROMOTIONAL_BROADCAST_RE = re.compile(
    r"(?:подписчик|просмотр|реакци|лайк|репост|накрутк|заказы\s+запускаются\s+автоматически|"
    r"пополнение\s+через|залетайте\s+и\s+пробуйте|подпишитесь\s+на\s+канал)",
    re.IGNORECASE,
)
REFUSAL_OR_THANKS_RE = re.compile(
    r"^(?!.*\?)(?:(?:нет|неа|не|скорее\s+всего\s+нет|наверное[^.]{0,40}\sнет)\b.{0,100}|"
    r"(?:спасибо|благодарю|большое\s+спасибо|понял[аи]?[,! ]+благодарю)\b.{0,100}|"
    r"(?:ок+е*|оке+|ок\s+спс|короче\s+я\s+понял|досвидос)\b.{0,60})$",
    re.IGNORECASE,
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
    if normalized and not any(character.isalnum() for character in normalized):
        return MessageRelevance("social", False, "non-verbal acknowledgement")
    if dialog_classification == "automated_account":
        return MessageRelevance("service", False, "automated Telegram account")
    if OTP_RE.search(normalized):
        return MessageRelevance("service", False, "authentication or confirmation code")
    if SERVICE_RE.search(normalized):
        return MessageRelevance("service", False, "automated group or subscription event")
    closing_candidate = re.sub(r"[,;:!?…—\-]+", " ", normalized)
    closing_candidate = " ".join(closing_candidate.split())
    if (
        CLOSING_RE.fullmatch(normalized)
        or REFUSAL_OR_THANKS_RE.fullmatch(normalized)
        or closing_candidate.casefold()
        in {
            "хорошо спасибо",
            "ок спасибо",
            "окей спасибо",
            "понятно спасибо",
            "понял спасибо",
            "поняла спасибо",
            "договорились спасибо",
        }
    ):
        return MessageRelevance("social", False, "dialogue closing or acknowledgement")
    ad_score = sum(bool(pattern.search(normalized)) for pattern in AD_MARKERS)
    if ad_score >= 2 or PROMOTIONAL_BROADCAST_RE.search(normalized):
        return MessageRelevance("advertising", False, "high-confidence promotional broadcast")
    return MessageRelevance("business", True, "potential business conversation")
