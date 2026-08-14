from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..analysis.schema import repair_json


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
    issue_family: Literal[
        "UNANSWERED_REQUEST",
        "TECHNICAL_PROBLEM",
        "COMMERCIAL_OPPORTUNITY",
        "PRODUCT_DISSATISFACTION",
        "PAYMENT_QUESTION",
        "FOLLOWUP",
        "PROMISE_DEADLINE",
        "HANDOFF",
        "OTHER",
    ] | None = None
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
