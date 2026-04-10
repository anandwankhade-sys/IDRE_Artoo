# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Optional

from typing_extensions import TypedDict

from schemas.code_proposal import CodeProposal
from schemas.completeness import CompletenessResult
from schemas.confluence import ConfluenceContext
from schemas.plan import ImplementationPlan
from schemas.pr import PRCompositionResult
from schemas.repo import RepoContext
from schemas.test_suggestion import TestSuggestions
from schemas.ticket import TicketContext


class WorkflowPhase(str, Enum):
    FETCHING_TICKET = "fetching_ticket"
    CLASSIFYING_TICKET = "classifying_ticket"
    ASSEMBLING_CONTEXT = "assembling_context"
    CHECKING_COMPLETENESS = "checking_completeness"
    POSTING_CLARIFICATION = "posting_clarification"
    SCOUTING_REPO = "scouting_repo"
    FETCHING_CONFLUENCE_DOCS = "fetching_confluence_docs"
    EXPLORING_CODE = "exploring_code"
    PLANNING = "planning"
    CRITIQUING_PLAN = "critiquing_plan"
    CALIBRATING_SCOPE = "calibrating_scope"
    PROPOSING_CODE = "proposing_code"
    VALIDATING_OUTPUT = "validating_output"
    SUGGESTING_TESTS = "suggesting_tests"
    COMPOSING_PR = "composing_pr"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowState(TypedDict, total=False):
    # ── Identity ─────────────────────────────────────────────────────────────
    run_id: str           # UUID for this specific workflow run
    ticket_id: str        # Jira issue key (e.g. PROJ-123)
    started_at: str       # ISO-8601 UTC timestamp

    # ── Agent outputs (populated progressively) ──────────────────────────────
    ticket_context: Optional[TicketContext]
    completeness_result: Optional[CompletenessResult]
    repo_context: Optional[RepoContext]
    confluence_context: Optional[ConfluenceContext]
    implementation_plan: Optional[ImplementationPlan]
    code_proposal: Optional[CodeProposal]
    test_suggestions: Optional[TestSuggestions]
    pr_result: Optional[PRCompositionResult]

    # ── Routing / control flow ────────────────────────────────────────────────
    current_phase: WorkflowPhase
    is_complete_ticket: Optional[bool]  # Set by completeness_check node
    should_stop: bool                   # Emergency stop flag

    # ── Append-only audit lists (LangGraph reducer) ───────────────────────────
    errors: Annotated[list[str], operator.add]
    llm_call_ids: Annotated[list[str], operator.add]
    mcp_tool_calls: Annotated[list[dict], operator.add]

    # ── Hybrid pipeline additions ─────────────────────────────────────────────
    ticket_type: Optional[str]           # "bug_fix", "feature", "ui_change", "refactor", "other"
    assembled_context: Optional[dict]    # From context_assembler: repo_map_section, file_summaries, etc.
    exploration_context: Optional[str]   # From explorer_agent: code snippets found
    plan_critique: Optional[dict]        # From plan_critic: {approved, feedback, revision_count}
    scope_check: Optional[dict]          # From scope_calibrator: {within_scope, ratio, warning}
    validation_result: Optional[dict]    # From validation_agent: {passed, confidence_gate, issues}
    plan_revision_count: int             # Tracks how many times plan has been revised (default 0)

    # ── Summary counters ──────────────────────────────────────────────────────
    completed_at: Optional[str]
    total_llm_calls: int
    total_tokens_used: int
