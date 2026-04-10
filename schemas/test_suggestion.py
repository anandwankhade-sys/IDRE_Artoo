# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class TestType(str, Enum):
    UNIT = "unit"
    INTEGRATION = "integration"
    CONTRACT = "contract"
    E2E = "e2e"


class TestCase(BaseModel):
    test_name: str
    test_type: TestType = TestType.UNIT
    target_function_or_class: str
    description: str
    arrange: str = Field(..., description="Setup / preconditions")
    act: str = Field(..., description="Action being tested")
    assert_description: str = Field(..., description="Expected outcome / assertion")
    edge_case: bool = False
    mock_dependencies: list[str] = Field(default_factory=list)
    sample_code: Optional[str] = Field(
        default=None,
        description="Optional illustrative test code snippet",
    )


class TestSuggestions(BaseModel):
    ticket_id: str
    framework: str = Field(default="pytest", description="Suggested test framework")
    suggested_test_file_paths: list[str] = Field(default_factory=list)
    test_cases: list[TestCase] = Field(default_factory=list)
    coverage_targets: list[str] = Field(
        default_factory=list,
        description="Functions/classes that must be covered",
    )
    test_fixtures_needed: list[str] = Field(default_factory=list)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    raw_llm_response: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_json_strings(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for field in (
            "suggested_test_file_paths",
            "test_cases",
            "coverage_targets",
            "test_fixtures_needed",
        ):
            val = values.get(field)
            if isinstance(val, str):
                try:
                    values[field] = json.loads(val, strict=False)
                except (json.JSONDecodeError, ValueError):
                    values[field] = []
        return values
