from __future__ import annotations

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


def parse_triage_result(raw: str) -> tuple[TriageResult, bool]:
    try:
        return TriageResult.model_validate_json(raw), False
    except ValidationError:
        repaired = repair_json(raw)
        return TriageResult.model_validate_json(repaired), repaired != raw
