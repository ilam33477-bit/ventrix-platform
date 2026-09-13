from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..analysis.schema import repair_json

CONVERSATION_STATES = {
    "WAITING_FOR_EMPLOYEE",
    "WAITING_FOR_CLIENT",
    "CLOSED_SUCCESS",
    "CLOSED_REJECTED",
    "CLOSED_NEUTRAL",
    "ACTIVE_SUPPORT",
    "ACTIVE_SALES",
    "FOLLOWUP_LATER",
    "AMBIGUOUS",
}
ISSUE_FAMILIES = {
    "UNANSWERED_REQUEST",
    "TECHNICAL_PROBLEM",
    "COMMERCIAL_OPPORTUNITY",
    "PRODUCT_DISSATISFACTION",
    "PAYMENT_QUESTION",
    "FOLLOWUP",
    "PROMISE_DEADLINE",
    "HANDOFF",
    "OTHER",
}


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criticality: int = Field(ge=0, le=100)
    category: str = Field(min_length=1, max_length=64)
    requires_immediate_attention: bool
    requires_employee_notification: bool
    requires_manager_notification: bool
    reason: str = Field(min_length=1, max_length=2000)
    recommended_action: str = Field(min_length=1, max_length=2000)
    recommended_deadline_minutes: int | None = Field(default=None, ge=1, le=43_200)
    needs_deep_analysis: bool
    message_class: str = Field(default="business", max_length=32)
    business_relevance: bool = True
    conversation_state: Literal[
        "WAITING_FOR_EMPLOYEE",
        "WAITING_FOR_CLIENT",
        "CLOSED_SUCCESS",
        "CLOSED_REJECTED",
        "CLOSED_NEUTRAL",
        "ACTIVE_SUPPORT",
        "ACTIVE_SALES",
        "FOLLOWUP_LATER",
        "AMBIGUOUS",
    ] = "AMBIGUOUS"
    response_required: bool = True
    action_required: bool = True
    issue_family: (
        Literal[
            "UNANSWERED_REQUEST",
            "TECHNICAL_PROBLEM",
            "COMMERCIAL_OPPORTUNITY",
            "PRODUCT_DISSATISFACTION",
            "PAYMENT_QUESTION",
            "FOLLOWUP",
            "PROMISE_DEADLINE",
            "HANDOFF",
            "OTHER",
        ]
        | None
    ) = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    client_intent: str = Field(default="UNKNOWN", max_length=100)
    last_meaningful_client_message: str | None = Field(default=None, max_length=2000)
    evidence_message_ids: list[str | int] = Field(default_factory=list)
    close_existing_issue_families: list[str] = Field(default_factory=list)
    followup_at: str | None = None


def parse_triage_result(raw: str) -> tuple[TriageResult, bool]:
    try:
        return TriageResult.model_validate_json(raw), False
    except ValidationError:
        repaired = repair_json(raw)
        return TriageResult.model_validate_json(repaired), repaired != raw


def parse_triage_result_lenient(raw: str) -> TriageResult:
    """Normalize a provider JSON object after strict schema validation fails.

    This deliberately uses conservative defaults: an omitted action/response flag does not
    create a client problem. Non-JSON output still fails so the queue can retry it normally.
    """

    payload = json.loads(repair_json(raw))
    if not isinstance(payload, dict):
        raise TypeError("triage response must be a JSON object")

    def text(name: str, default: str, limit: int) -> str:
        value = payload.get(name)
        rendered = str(value).strip() if value is not None else ""
        return (rendered or default)[:limit]

    def boolean(name: str, default: bool = False) -> bool:
        value = payload.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "yes", "1", "да"}:
                return True
            if normalized in {"false", "no", "0", "нет", "null", "none", ""}:
                return False
        return default

    def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(float(payload.get(name, default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def bounded_float(name: str, default: float) -> float:
        try:
            value = float(payload.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(0.0, min(1.0, value))

    def optional_deadline() -> int | None:
        value = payload.get("recommended_deadline_minutes")
        if value is None or (isinstance(value, str) and value.strip().casefold() in {"", "null"}):
            return None
        try:
            return max(1, min(43_200, int(float(value))))
        except (TypeError, ValueError):
            return None

    def identifiers(name: str) -> list[str | int]:
        value = payload.get(name)
        if not isinstance(value, list):
            return []
        return [
            item for item in value if isinstance(item, (str, int)) and not isinstance(item, bool)
        ]

    state = text("conversation_state", "AMBIGUOUS", 40).upper().replace("-", "_").replace(" ", "_")
    if state not in CONVERSATION_STATES:
        state = "AMBIGUOUS"

    raw_family = payload.get("issue_family")
    family = (
        str(raw_family).strip().upper().replace("-", "_").replace(" ", "_")
        if raw_family is not None
        else None
    )
    if family not in ISSUE_FAMILIES:
        family = None

    close_families = []
    raw_close = payload.get("close_existing_issue_families")
    if isinstance(raw_close, list):
        close_families = [
            normalized
            for value in raw_close
            if isinstance(value, str)
            and (normalized := value.strip().upper().replace("-", "_").replace(" ", "_"))
            in ISSUE_FAMILIES
        ]

    followup = payload.get("followup_at")
    last_message = payload.get("last_meaningful_client_message")
    return TriageResult(
        criticality=bounded_int("criticality", 0, 0, 100),
        category=text("category", "other", 64),
        requires_immediate_attention=boolean("requires_immediate_attention"),
        requires_employee_notification=boolean("requires_employee_notification"),
        requires_manager_notification=boolean("requires_manager_notification"),
        reason=text(
            "reason",
            "AI-ответ восстановлен после ошибки формата; требуется консервативная проверка.",
            2000,
        ),
        recommended_action=text(
            "recommended_action",
            "Проверить диалог при следующем обновлении.",
            2000,
        ),
        recommended_deadline_minutes=optional_deadline(),
        needs_deep_analysis=boolean("needs_deep_analysis"),
        message_class=text("message_class", "uncertain", 32),
        business_relevance=boolean("business_relevance"),
        conversation_state=state,
        response_required=boolean("response_required"),
        action_required=boolean("action_required"),
        issue_family=family,
        confidence=bounded_float("confidence", 0.5),
        client_intent=text("client_intent", "UNKNOWN", 100),
        last_meaningful_client_message=(
            str(last_message).strip()[:2000] if last_message is not None else None
        ),
        evidence_message_ids=identifiers("evidence_message_ids"),
        close_existing_issue_families=close_families,
        followup_at=str(followup).strip() if followup is not None else None,
    )
