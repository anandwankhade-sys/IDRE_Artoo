# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class FileAnalysis(BaseModel):
    file_path: str
    language: Optional[str] = None
    relevance_score: float = Field(..., ge=0.0, le=1.0)
    relevance_reason: str
    summary: Optional[str] = None
    functions_detected: list[str] = Field(default_factory=list)
    classes_detected: list[str] = Field(default_factory=list)


class RepoContext(BaseModel):
    repo_owner: str
    repo_name: str
    default_branch: str = "main"
    primary_language: Optional[str] = None
    directory_summary: str = Field(
        ...,
        description="Top-level directory structure as text",
    )
    relevant_files: list[FileAnalysis] = Field(default_factory=list)
    existing_test_files: list[str] = Field(default_factory=list)
    dependency_hints: list[str] = Field(
        default_factory=list,
        description="Key dependencies from package.json, requirements.txt, etc.",
    )
    code_style_hints: Optional[str] = Field(
        default=None,
        description="Inferred code style: type hints, docstrings, formatting",
    )
    impacted_modules: list[str] = Field(default_factory=list)
    raw_mcp_outputs: list[dict] = Field(
        default_factory=list,
        description="Raw GitHub MCP tool call outputs stored for audit",
    )
