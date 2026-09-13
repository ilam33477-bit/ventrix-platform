from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DetectedProblem(BaseModel):
    # Provider-added explanatory keys are harmless; rejecting the entire batch
    # for them caused expensive retries and occasional terminal failures.
    model_config = ConfigDict(extra="ignore")

    event_type: str = Field(min_length=1, max_length=64)
    is_problem: bool
    priority: Literal["informational", "low", "medium", "high", "critical"]
    confidence: float = Field(ge=0, le=1)
    requires_review: bool = False
    source_message_ids: list[str | int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=2000)
    recommended_action: str = Field(min_length=1, max_length=2000)
    conversation_state: str = Field(default="AMBIGUOUS", max_length=40)
    response_required: bool = True
    action_required: bool = True
    issue_family: str | None = Field(default=None, max_length=64)
    evidence_message_ids: list[str | int] = Field(default_factory=list)
    close_existing_issue_families: list[str] = Field(default_factory=list)


class BusinessOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outcome_type: Literal[
        "interest_confirmed",
        "call_scheduled",
        "sale_confirmed",
        "follow_up_agreed",
    ]
    explicitly_supported: bool = False
    confidence: float = Field(ge=0, le=1)
    source_message_ids: list[str | int] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=1000)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=12)


class DialogAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chat_id: str
    dialog_type: str
    summary: str
    participants: list[str | int] = Field(default_factory=list)
    detected_patterns: list[str] = Field(default_factory=list)
    problems: list[DetectedProblem] = Field(default_factory=list)
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)


class AIUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.0"]
    tenant_id: str
    batch_id: str
    dialog_results: list[DialogAnalysisResult]
    usage: AIUsage = Field(default_factory=AIUsage)


class ReportEmployeeNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    employee_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=1200)


class ReportDialogNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dialog: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=1200)


class ReportNarrative(BaseModel):
    """Strict client-visible report copy returned by the optional AI provider."""

    model_config = ConfigDict(extra="ignore")

    executive_summary: str = Field(min_length=1, max_length=2400)
    highlights: list[str] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=12)
    employee_notes: list[ReportEmployeeNote] = Field(default_factory=list, max_length=30)
    dialog_notes: list[ReportDialogNote] = Field(default_factory=list, max_length=30)
    recommendations: list[str] = Field(default_factory=list, max_length=12)


def repair_json(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end >= start:
        value = value[start : end + 1]
    value = re.sub(r",\s*([}\]])", r"\1", value)
    return value


def parse_analysis_response(
    raw: str, *, allow_repair: bool = True
) -> tuple[AnalysisResponse, bool]:
    try:
        return AnalysisResponse.model_validate_json(raw), False
    except (ValidationError, json.JSONDecodeError):
        if not allow_repair:
            raise
    repaired = repair_json(raw)
    return AnalysisResponse.model_validate_json(repaired), repaired != raw


def normalize_analysis_response(
    raw: str,
    *,
    tenant_id: str,
    batch_id: str,
    expected_chat_ids: set[str],
) -> AnalysisResponse:
    """Recover a syntactically valid provider response without inventing issues.

    Provider schema drift in one candidate must not discard every valid dialog in
    the batch. Invalid problem/outcome candidates are omitted; missing expected
    dialogs become neutral results so identity validation still protects scope.
    """

    payload = json.loads(repair_json(raw))
    if not isinstance(payload, dict):
        raise TypeError("AI response must be a JSON object")

    raw_results = payload.get("dialog_results")
    if not isinstance(raw_results, list):
        raw_results = []
    results_by_chat: dict[str, DialogAnalysisResult] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("chat_id", ""))
        if chat_id not in expected_chat_ids or chat_id in results_by_chat:
            continue
        problems: list[DetectedProblem] = []
        for candidate in item.get("problems", []):
            if not isinstance(candidate, dict):
                continue
            try:
                problems.append(DetectedProblem.model_validate(candidate))
            except ValidationError:
                continue
        outcomes: list[BusinessOutcome] = []
        for candidate in item.get("business_outcomes", []):
            if not isinstance(candidate, dict):
                continue
            try:
                outcomes.append(BusinessOutcome.model_validate(candidate))
            except ValidationError:
                continue
        participants = item.get("participants", [])
        patterns = item.get("detected_patterns", [])
        results_by_chat[chat_id] = DialogAnalysisResult(
            chat_id=chat_id,
            dialog_type=str(item.get("dialog_type") or "unknown"),
            summary=str(item.get("summary") or ""),
            participants=participants if isinstance(participants, list) else [],
            detected_patterns=patterns if isinstance(patterns, list) else [],
            problems=problems,
            business_outcomes=outcomes,
        )

    dialog_results = [
        results_by_chat.get(chat_id)
        or DialogAnalysisResult(chat_id=chat_id, dialog_type="unknown", summary="")
        for chat_id in sorted(expected_chat_ids)
    ]
    usage = payload.get("usage")
    try:
        normalized_usage = AIUsage.model_validate(usage if isinstance(usage, dict) else {})
    except ValidationError:
        normalized_usage = AIUsage()
    return AnalysisResponse(
        schema_version="1.0",
        tenant_id=tenant_id,
        batch_id=batch_id,
        dialog_results=dialog_results,
        usage=normalized_usage,
    )
