# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/ticket_classifier.py
============================
Deterministic (no LLM) ticket type classifier.

Classifies a JIRA ticket into one of five categories based on simple keyword
matching against the ticket title and description.  Rules are applied in
priority order — the first matching rule wins.

Categories (in order of precedence):
  bug_fix   — defect / something broken
  feature   — new capability being added
  ui_change — visual / frontend change
  refactor  — code quality / improvement
  other     — catch-all
"""

from __future__ import annotations

from schemas.workflow_state import WorkflowState

# ---------------------------------------------------------------------------
# Classification rules (evaluated in the order listed below)
# ---------------------------------------------------------------------------

_RULES: list[tuple[str, list[str]]] = [
    (
        "bug_fix",
        [
            "fix", "bug", "error", "failing", "not able",
            "incorrect", "wrong", "broken", "issue", "not working",
            "unable", "cannot", "still able to see", "still showing",
            "still visible", "should not", "shouldn't", "does not",
            "doesn't", "missing", "unexpected", "unintended",
            "regression", "defect", "crash", "exception",
        ],
    ),
    (
        "feature",
        [
            "add", "implement", "create", "new", "build",
            "develop", "support",
        ],
    ),
    (
        "ui_change",
        [
            "display", "show", "view", "ui", "button",
            "dropdown", "dashboard", "portal",
        ],
    ),
    (
        "refactor",
        [
            "refactor", "cleanup", "improve", "optimize",
            "update", "adjust", "enhance",
        ],
    ),
]

_DEFAULT_TYPE = "other"


def classify_ticket(title: str, description: str) -> str:
    """
    Classify a JIRA ticket from its *title* and *description*.

    Matching is case-insensitive and operates on the combined text of both
    fields.  The first rule whose keyword list contains any match wins.

    Returns one of: ``bug_fix``, ``feature``, ``ui_change``,
    ``refactor``, ``other``.
    """
    combined = f"{title} {description}".lower()

    for ticket_type, keywords in _RULES:
        for kw in keywords:
            if kw in combined:
                return ticket_type

    return _DEFAULT_TYPE


def ticket_classifier_node(state: WorkflowState) -> dict:
    """
    LangGraph node — classifies the ticket type deterministically.

    Reads:
        state["ticket_context"] — TicketContext (title + description)

    Returns:
        {"ticket_type": str}  — one of bug_fix / feature / ui_change /
                                refactor / other
    """
    ticket_context = state.get("ticket_context")

    if ticket_context is None:
        return {"ticket_type": _DEFAULT_TYPE}

    title = getattr(ticket_context, "title", "") or ""
    description = getattr(ticket_context, "description", "") or ""

    ticket_type = classify_ticket(title, description)
    return {"ticket_type": ticket_type}
