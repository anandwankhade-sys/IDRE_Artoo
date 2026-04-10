# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class ChangeType(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class FileDiff(BaseModel):
    file_path: str
    change_type: ChangeType
    original_content_snippet: Optional[str] = Field(
        default=None,
        description="Relevant excerpt from original file",
    )
    proposed_content: str = Field(
        ...,
        description="Full proposed content or unified diff patch",
    )
    is_diff_format: bool = Field(
        default=True,
        description="True if proposed_content is a unified diff, False if full file",
    )
    rationale: str


class CodeProposal(BaseModel):
    ticket_id: str
    summary: str
    file_changes: list[FileDiff] = Field(default_factory=list)
    new_dependencies: list[str] = Field(default_factory=list)
    configuration_changes: list[str] = Field(default_factory=list)
    migration_scripts: list[str] = Field(default_factory=list)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    caveats: list[str] = Field(default_factory=list)
    raw_llm_response: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_json_strings(cls, values: Any) -> Any:
        """AWS Bedrock function-calling sometimes serialises nested arrays and
        objects as JSON strings instead of native JSON.  This validator detects
        that pattern and parses the string back into the expected Python type
        before Pydantic runs field-level validation."""
        if not isinstance(values, dict):
            return values
        list_fields = (
            "file_changes",
            "new_dependencies",
            "configuration_changes",
            "migration_scripts",
            "caveats",
        )
        for field in list_fields:
            val = values.get(field)
            if isinstance(val, str):
                try:
                    # strict=False allows literal control characters (e.g. raw
                    # newlines inside proposed_content) that Bedrock sometimes
                    # emits when serialising file diffs as a JSON string.
                    values[field] = json.loads(val, strict=False)
                except (json.JSONDecodeError, ValueError):
                    # JSON is structurally broken (unescaped inner quotes from
                    # Bedrock double-serialising code content). Fall back to
                    # empty list so the rest of CodeProposal can still be
                    # created and the pipeline continues without a hard crash.
                    values[field] = []
        return values
