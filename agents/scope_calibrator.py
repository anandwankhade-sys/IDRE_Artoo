# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/scope_calibrator.py
===========================
Deterministic (no LLM) scope-proportionality check.

Compares the number of files proposed in the implementation plan against a
pre-computed baseline for the detected ticket type.  If the plan proposes
more than 2.5x the baseline average, it is flagged as out-of-scope.

This node runs after the plan critic and before code proposal to catch
runaway plans that passed critique but are still disproportionately large.
"""

from __future__ import annotations

from app_logging.activity_logger import ActivityLogger
from code_intelligence.knowledge_base import get_scope_baseline
from schemas.workflow_state import WorkflowState

logger = ActivityLogger("scope_calibrator")

# Ratio threshold above which the plan is flagged as out-of-scope
_SCOPE_RATIO_THRESHOLD = 2.5

# Fallback baselines if the knowledge base is unavailable
_DEFAULT_BASELINES: dict[str, dict] = {
    "bug_fix":   {"avg_files": 2, "min_files": 1, "max_files": 5,  "ticket_type": "bug_fix"},
    "feature":   {"avg_files": 5, "min_files": 2, "max_files": 12, "ticket_type": "feature"},
    "ui_change": {"avg_files": 3, "min_files": 1, "max_files": 7,  "ticket_type": "ui_change"},
    "refactor":  {"avg_files": 4, "min_files": 2, "max_files": 10, "ticket_type": "refactor"},
    "other":     {"avg_files": 3, "min_files": 1, "max_files": 8,  "ticket_type": "other"},
}


def _collect_proposed_files(implementation_plan) -> set[str]:
    """
    Gather the union of all ``affected_files`` across every implementation
    step.  Returns an empty set if the plan is None or has no steps.
    """
    if implementation_plan is None:
        return set()

    files: set[str] = set()
    for step in getattr(implementation_plan, "implementation_steps", []):
        for f in getattr(step, "affected_files", []):
            if f:
                files.add(f)
    return files


def _get_baseline(ticket_type: str) -> dict:
    """
    Fetch the scope baseline for *ticket_type* from the knowledge base.
    Falls back to the hard-coded defaults if the knowledge base is unavailable
    or returns an empty/None result.
    """
    try:
        baseline = get_scope_baseline(ticket_type)
        if baseline and isinstance(baseline, dict) and "avg_files" in baseline:
            return baseline
    except Exception:
        pass

    return _DEFAULT_BASELINES.get(ticket_type, _DEFAULT_BASELINES["other"])


def scope_calibrator_node(state: WorkflowState) -> dict:
    """
    LangGraph node — checks whether the plan's scope is proportional to the
    ticket type.

    Reads:
        state["implementation_plan"]  — ImplementationPlan
        state["ticket_type"]          — str
        state["plan_critique"]        — dict (optional; used for logging context)

    Returns:
        {
            "scope_check": {
                "within_scope":   bool,
                "ratio":          float,   # proposed / baseline avg
                "proposed_count": int,
                "baseline":       dict,
                "warning":        str | None,
            }
        }
    """
    implementation_plan = state.get("implementation_plan")
    ticket_type = state.get("ticket_type") or "other"
    ticket_id = state.get("ticket_id", "unknown")
    run_id = state.get("run_id", "unknown")

    logger.info(
        "agent_node_entered",
        ticket_id=ticket_id,
        run_id=run_id,
        ticket_type=ticket_type,
        phase="scope_calibration",
    )

    # ── 1. Count proposed files ───────────────────────────────────────────────
    proposed_files = _collect_proposed_files(implementation_plan)
    proposed_count = len(proposed_files)

    # ── 2. Fetch baseline ─────────────────────────────────────────────────────
    baseline = _get_baseline(ticket_type)
    avg_files: float = baseline.get("avg_files", 3)

    # ── 3. Compute ratio ──────────────────────────────────────────────────────
    if avg_files > 0:
        ratio = proposed_count / avg_files
    else:
        ratio = 0.0

    # ── 4. Determine scope verdict ────────────────────────────────────────────
    if ratio > _SCOPE_RATIO_THRESHOLD:
        within_scope = False
        warning = (
            f"Proposing {ratio:.1f}x more files than the baseline average for a "
            f"'{ticket_type}' ticket (proposed: {proposed_count}, "
            f"baseline avg: {avg_files:.0f}).  Consider narrowing the scope."
        )
    else:
        within_scope = True
        warning = None

    scope_check: dict = {
        "within_scope": within_scope,
        "ratio": round(ratio, 2),
        "proposed_count": proposed_count,
        "baseline": baseline,
        "warning": warning,
    }

    logger.info(
        "agent_node_completed",
        ticket_id=ticket_id,
        run_id=run_id,
        within_scope=within_scope,
        ratio=round(ratio, 2),
        proposed_count=proposed_count,
        avg_files=avg_files,
        warning=warning,
    )

    return {"scope_check": scope_check}
