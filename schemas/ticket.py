# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    READY_FOR_DEV = "Ready for Dev"
    IN_PROGRESS = "In Progress"
    NEEDS_CLARIFICATION = "needs-clarification"
    TODO = "To Do"
    DONE = "Done"


class JiraAttachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int = 0
    url: Optional[str] = None


class TicketContext(BaseModel):
    ticket_id: str = Field(..., description="Jira issue key, e.g. PROJ-123")
    title: str
    description: str
    acceptance_criteria: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    priority: Optional[str] = None
    story_points: Optional[float] = None
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    status: str = TicketStatus.READY_FOR_DEV
    attachments: list[JiraAttachment] = Field(default_factory=list)
    linked_issues: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    raw_jira_data: Optional[dict] = Field(
        default=None,
        description="Full raw Jira API response stored for audit",
    )
