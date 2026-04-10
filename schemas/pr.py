# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PRStatus(str, Enum):
    CREATED = "created"
    FAILED = "failed"
    SKIPPED = "skipped"


class PRCompositionResult(BaseModel):
    ticket_id: str
    status: PRStatus
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    branch_name: Optional[str] = None
    base_branch: str = "main"
    pr_title: str
    pr_body: str = ""
    draft: bool = True
    reviewers_requested: list[str] = Field(default_factory=list)
    labels_applied: list[str] = Field(default_factory=list)
    jira_ticket_linked: bool = False
    error_message: Optional[str] = None
    raw_mcp_output: Optional[dict] = None
