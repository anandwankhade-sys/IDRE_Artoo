# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class CompletenessDecision(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    BORDERLINE = "borderline"


class MissingField(BaseModel):
    field_name: str
    severity: str = Field(..., description="critical | major | minor")
    description: str


class CompletenessResult(BaseModel):
    ticket_id: str
    decision: CompletenessDecision
    completeness_score: float = Field(..., ge=0.0, le=1.0)
    missing_fields: list[MissingField] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    assumptions_summary: Optional[str] = None
    jira_comment_posted: bool = False
    jira_comment_id: Optional[str] = None
    raw_llm_response: Optional[str] = Field(
        default=None,
        description="Raw LLM output before parsing, stored for audit",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_json_strings(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for field in ("missing_fields", "clarification_questions"):
            val = values.get(field)
            if isinstance(val, str):
                try:
                    values[field] = json.loads(val, strict=False)
                except (json.JSONDecodeError, ValueError):
                    values[field] = []
        return values
