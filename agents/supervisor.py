# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/supervisor.py — Hybrid Pipeline
=======================================
Builds and runs the LangGraph StateGraph for the IDRE Artoo hybrid pipeline.

Topology:
  START → fetch_ticket → ticket_classifier → context_assembler → repo_scout
        → confluence_docs → completeness_check
          ├─ (incomplete/error) → post_clarification → end_workflow
          └─ (complete) → explorer → planner → plan_critic → scope_calibrator
                                                    ↑                │
                         [revise & count < 2] ──────┘    ┌───────────┘
                                                         │
                               [approved & within scope] → code_proposal → validation
                                                                              │
                               [passed/flag] → file_validator → test_suggestion → pr_composer → end
                               [block & count < 2] → bump_revision → planner (loop back)
                               [block & count >= 2] → end_workflow (with warning)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from langgraph.graph import END, START, StateGraph

from agents.completeness_agent import completeness_check_node, post_clarification_node
from agents.code_proposal_agent import code_proposal_node
from agents.confluence_agent import confluence_agent_node
from agents.context_assembler import context_assembler_node
from agents.explorer_agent import explorer_node
from agents.file_validator_agent import file_validator_node
from agents.plan_critic import plan_critic_node
from agents.planner_agent import planner_node
from agents.pr_composer_agent import pr_composer_node
from agents.repo_scout_agent import repo_scout_node
from agents.scope_calibrator import scope_calibrator_node
from agents.test_agent import test_suggestion_node
from agents.ticket_classifier import ticket_classifier_node
from agents.ticket_fetcher import fetch_ticket_node
from agents.validation_agent import validation_node
from app_logging.activity_logger import ActivityLogger
from persistence.database import init_db
from persistence.repository import TicketRepository
from schemas.workflow_state import WorkflowPhase, WorkflowState

logger = ActivityLogger("supervisor")
_repo = TicketRepository()


# ── Live progress tracking ───────────────────────────────────────────────────

# Map node names to the WorkflowPhase they represent
_NODE_PHASE_MAP = {
    "fetch_ticket": WorkflowPhase.FETCHING_TICKET,
    "ticket_classifier": WorkflowPhase.CLASSIFYING_TICKET,
    "context_assembler": WorkflowPhase.ASSEMBLING_CONTEXT,
    "repo_scout": WorkflowPhase.SCOUTING_REPO,
    "confluence_docs": WorkflowPhase.FETCHING_CONFLUENCE_DOCS,
    "completeness_check": WorkflowPhase.CHECKING_COMPLETENESS,
    "post_clarification": WorkflowPhase.POSTING_CLARIFICATION,
    "explorer": WorkflowPhase.EXPLORING_CODE,
    "planner": WorkflowPhase.PLANNING,
    "plan_critic": WorkflowPhase.CRITIQUING_PLAN,
    "scope_calibrator": WorkflowPhase.CALIBRATING_SCOPE,
    "code_proposal": WorkflowPhase.PROPOSING_CODE,
    "validation": WorkflowPhase.VALIDATING_OUTPUT,
    "file_validator": WorkflowPhase.VALIDATING_OUTPUT,
    "test_suggestion": WorkflowPhase.SUGGESTING_TESTS,
    "pr_composer": WorkflowPhase.COMPOSING_PR,
    "end_workflow": WorkflowPhase.COMPLETED,
}


def _update_progress(run_id: str, node_name: str, state: dict) -> None:
    """Update the DB with live progress after each node completes."""
    phase = _NODE_PHASE_MAP.get(node_name)
    if not phase or not run_id:
        return

    updates = {"current_phase": phase.value}

    # Update intermediate result fields as they become available
    completeness = state.get("completeness_result")
    if completeness:
        updates["completeness_score"] = completeness.completeness_score
        updates["ticket_deemed_incomplete"] = (
            completeness.decision.value == "incomplete" if hasattr(completeness.decision, "value") else False
        )

    if state.get("implementation_plan") is not None:
        updates["implementation_plan_generated"] = True
    if state.get("code_proposal") is not None:
        updates["code_proposal_generated"] = True
    if state.get("test_suggestions") is not None:
        updates["tests_suggested"] = True

    pr_result = state.get("pr_result")
    if pr_result and hasattr(pr_result, "pr_url") and pr_result.pr_url:
        updates["pr_url"] = pr_result.pr_url
        updates["pr_number"] = pr_result.pr_number
        updates["pr_branch"] = pr_result.branch_name
        updates["pr_outcome"] = "pending"

    try:
        _repo.update_run(run_id, **updates)
    except Exception as exc:
        logger.warning("progress_update_failed", node=node_name, exc=str(exc))


def _wrap_node(node_name: str, node_fn):
    """Wrap a node function to update DB progress after it runs."""
    def wrapped(state: WorkflowState) -> dict:
        result = node_fn(state)
        # Merge result into state for progress check
        merged = {**state, **(result or {})}
        run_id = state.get("run_id", "")
        _update_progress(run_id, node_name, merged)
        return result
    wrapped.__name__ = node_fn.__name__
    return wrapped


# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_completeness(
    state: WorkflowState,
) -> Literal["post_clarification", "explorer"]:
    if state.get("should_stop"):
        return "post_clarification"
    if state.get("is_complete_ticket"):
        return "explorer"

    # Bug tickets are naturally terse — let them through if they have at least
    # a title.  The explorer + planner can work with minimal descriptions.
    ticket_type = state.get("ticket_type", "other")
    ticket_context = state.get("ticket_context")
    if ticket_type == "bug_fix" and ticket_context and getattr(ticket_context, "title", ""):
        logger.info(
            "completeness_bypassed_for_bug",
            ticket_id=state.get("ticket_id", "?"),
            reason="bug tickets proceed with minimal info",
        )
        return "explorer"

    return "post_clarification"


def route_after_scope(
    state: WorkflowState,
) -> Literal["code_proposal", "bump_revision_to_planner"]:
    plan_critique = state.get("plan_critique") or {}
    scope_check = state.get("scope_check") or {}
    revision_count = state.get("plan_revision_count", 0)

    approved = plan_critique.get("approved", False)
    within_scope = scope_check.get("within_scope", True)

    if approved and within_scope:
        return "code_proposal"
    if revision_count < 2:
        return "bump_revision_to_planner"

    # Exhausted revision budget — proceed anyway
    logger.warning(
        "revision_budget_exhausted",
        ticket_id=state.get("ticket_id", "?"),
        revision_count=revision_count,
    )
    return "code_proposal"


def route_after_validation(
    state: WorkflowState,
) -> Literal["file_validator", "bump_revision_to_planner", "end_workflow"]:
    validation_result = state.get("validation_result") or {}
    gate = validation_result.get("confidence_gate", "block")
    revision_count = state.get("plan_revision_count", 0)

    if gate in ("proceed", "flag_for_review"):
        return "file_validator"
    if revision_count < 2:
        return "bump_revision_to_planner"
    return "end_workflow"


# ── Helper nodes ──────────────────────────────────────────────────────────────

def bump_revision_node(state: WorkflowState) -> dict:
    """Increment revision counter before looping back to planner."""
    current = state.get("plan_revision_count", 0)
    return {
        "plan_revision_count": current + 1,
        "current_phase": WorkflowPhase.PLANNING,
    }


def end_workflow_node(state: WorkflowState) -> dict:
    """Final node: record completion timestamp."""
    return {
        "current_phase": WorkflowPhase.COMPLETED,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(WorkflowState)

    # Register all nodes (wrapped for live progress tracking)
    graph.add_node("fetch_ticket", _wrap_node("fetch_ticket", fetch_ticket_node))
    graph.add_node("ticket_classifier", _wrap_node("ticket_classifier", ticket_classifier_node))
    graph.add_node("context_assembler", _wrap_node("context_assembler", context_assembler_node))
    graph.add_node("repo_scout", _wrap_node("repo_scout", repo_scout_node))
    graph.add_node("confluence_docs", _wrap_node("confluence_docs", confluence_agent_node))
    graph.add_node("completeness_check", _wrap_node("completeness_check", completeness_check_node))
    graph.add_node("post_clarification", _wrap_node("post_clarification", post_clarification_node))
    graph.add_node("explorer", _wrap_node("explorer", explorer_node))
    graph.add_node("planner", _wrap_node("planner", planner_node))
    graph.add_node("plan_critic", _wrap_node("plan_critic", plan_critic_node))
    graph.add_node("scope_calibrator", _wrap_node("scope_calibrator", scope_calibrator_node))
    graph.add_node("code_proposal", _wrap_node("code_proposal", code_proposal_node))
    graph.add_node("validation", _wrap_node("validation", validation_node))
    graph.add_node("file_validator", _wrap_node("file_validator", file_validator_node))
    graph.add_node("test_suggestion", _wrap_node("test_suggestion", test_suggestion_node))
    graph.add_node("pr_composer", _wrap_node("pr_composer", pr_composer_node))
    graph.add_node("bump_revision_to_planner", _wrap_node("bump_revision_to_planner", bump_revision_node))
    graph.add_node("end_workflow", _wrap_node("end_workflow", end_workflow_node))

    # Entry sequence
    graph.add_edge(START, "fetch_ticket")
    graph.add_edge("fetch_ticket", "ticket_classifier")
    graph.add_edge("ticket_classifier", "context_assembler")
    graph.add_edge("context_assembler", "repo_scout")
    graph.add_edge("repo_scout", "confluence_docs")
    graph.add_edge("confluence_docs", "completeness_check")

    # Completeness branch
    graph.add_conditional_edges(
        "completeness_check",
        route_after_completeness,
        {
            "post_clarification": "post_clarification",
            "explorer": "explorer",
        },
    )
    graph.add_edge("post_clarification", "end_workflow")

    # Exploration → Planning → Review loop
    graph.add_edge("explorer", "planner")
    graph.add_edge("planner", "plan_critic")
    graph.add_edge("plan_critic", "scope_calibrator")

    graph.add_conditional_edges(
        "scope_calibrator",
        route_after_scope,
        {
            "code_proposal": "code_proposal",
            "bump_revision_to_planner": "bump_revision_to_planner",
        },
    )
    graph.add_edge("bump_revision_to_planner", "planner")

    # Code proposal → Validation gate
    graph.add_edge("code_proposal", "validation")
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {
            "file_validator": "file_validator",
            "bump_revision_to_planner": "bump_revision_to_planner",
            "end_workflow": "end_workflow",
        },
    )

    # Final sequence
    graph.add_edge("file_validator", "test_suggestion")
    graph.add_edge("test_suggestion", "pr_composer")
    graph.add_edge("pr_composer", "end_workflow")

    graph.add_edge("end_workflow", END)

    return graph.compile()


# Compiled graph (singleton)
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# ── Public entry point ────────────────────────────────────────────────────────

def run_workflow(ticket_id: str) -> WorkflowState:
    """
    Main entry point called by the scheduler and CLI.
    Initialises state, runs the LangGraph, persists result.
    Returns the final WorkflowState.
    """
    run_id = str(uuid.uuid4())

    global _graph
    _graph = None  # Force graph rebuild to pick up latest node wrappers

    init_db()
    _repo.create_run(run_id, ticket_id)
    _repo.mark_ticket_queued(ticket_id, run_id)

    initial_state: WorkflowState = {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_phase": WorkflowPhase.FETCHING_TICKET,
        "is_complete_ticket": None,
        "should_stop": False,
        "errors": [],
        "llm_call_ids": [],
        "mcp_tool_calls": [],
        "total_llm_calls": 0,
        "total_tokens_used": 0,
        # Hybrid fields
        "ticket_type": "other",
        "assembled_context": None,
        "exploration_context": None,
        "plan_critique": None,
        "scope_check": None,
        "validation_result": None,
        "plan_revision_count": 0,
    }

    logger.info("workflow_started", ticket_id=ticket_id, run_id=run_id, pipeline="hybrid")

    try:
        final_state = _get_graph().invoke(initial_state)
    except Exception as exc:
        logger.error("workflow_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
        final_state = {
            **initial_state,
            "current_phase": WorkflowPhase.FAILED,
            "errors": [str(exc)],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        _repo.finalize_run(run_id, final_state)
    except Exception as exc:
        logger.error("workflow_finalize_failed", exc=exc, run_id=run_id)

    phase = final_state.get("current_phase", WorkflowPhase.FAILED)
    errors = final_state.get("errors", [])

    logger.info(
        "workflow_completed",
        ticket_id=ticket_id,
        run_id=run_id,
        phase=str(phase),
        errors=errors,
        plan_revisions=final_state.get("plan_revision_count", 0),
        validation_gate=(final_state.get("validation_result") or {}).get("confidence_gate"),
        pr_url=(final_state.get("pr_result") or {}).get("pr_url") if isinstance(final_state.get("pr_result"), dict) else (
            final_state.get("pr_result").pr_url if final_state.get("pr_result") else None
        ),
    )

    return final_state
