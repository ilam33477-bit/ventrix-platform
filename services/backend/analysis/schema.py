from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DetectedProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=64)
    is_problem: bool
    priority: Literal["informational", "low", "medium", "high", "critical"]
    confidence: float = Field(ge=0, le=1)
    requires_review: bool = False
    source_message_ids: list[str | int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=2000)
    recommended_action: str = Field(min_length=1, max_length=2000)


class DialogAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str
    dialog_type: str
    summary: str
    participants: list[str | int] = Field(default_factory=list)
    detected_patterns: list[str] = Field(default_factory=list)
    problems: list[DetectedProblem] = Field(default_factory=list)


class AIUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    tenant_id: str
    batch_id: str
    dialog_results: list[DialogAnalysisResult]
    usage: AIUsage = Field(default_factory=AIUsage)


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
