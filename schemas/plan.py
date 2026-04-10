# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImplementationStep(BaseModel):
    step_number: int
    title: str
    description: str
    affected_files: list[str] = Field(default_factory=list)
    estimated_complexity: str = Field(
        ...,
        description="trivial | simple | moderate | complex",
    )


class ImplementationPlan(BaseModel):
    ticket_id: str
    summary: str
    impacted_components: list[str] = Field(default_factory=list)
    implementation_steps: list[ImplementationStep] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_rationale: str
    deployment_considerations: list[str] = Field(default_factory=list)
    breaking_changes: bool = False
    database_migrations_required: bool = False
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    raw_llm_response: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_json_strings(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for field in (
            "impacted_components",
            "implementation_steps",
            "deployment_considerations",
            "assumptions",
        ):
            val = values.get(field)
            if isinstance(val, str):
                try:
                    values[field] = json.loads(val, strict=False)
                except (json.JSONDecodeError, ValueError):
                    values[field] = []
        return values
