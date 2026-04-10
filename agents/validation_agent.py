# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/validation_agent.py
===========================
Final quality gate before PR composition.

Runs after code proposal has been generated and validates it against three
hard criteria:

1. **File existence** — every file in the code proposal must exist in the
   real codebase file index (using ``validate_proposed_paths``).
2. **Hallucination rate** — if > 30 % of proposed files are hallucinated,
   the confidence gate is set to "block" and the workflow stops.
3. **Confidence score** — the implementation plan's confidence score must
   be >= 0.4 to proceed unblocked.
4. **Minimum change requirement** — at least one file must be proposed.

Confidence gate decisions
--------------------------
  proceed         — confidence >= 0.65 AND hallucination_rate < 0.20
  flag_for_review — confidence >= 0.40 AND hallucination_rate < 0.40
  block           — everything else (triggers should_stop = True)

No LLM call is made — this node is fully deterministic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app_logging.activity_logger import ActivityLogger
from schemas.code_proposal import ChangeType, CodeProposal
from schemas.workflow_state import WorkflowState
from utils.file_index import validate_proposed_paths

logger = ActivityLogger("validation_agent")

# ── Thresholds ────────────────────────────────────────────────────────────────

_HALLUCINATION_BLOCK_THRESHOLD = 0.30   # > 30 % → block
_HALLUCINATION_FLAG_THRESHOLD = 0.20    # > 20 % → flag_for_review
_CONFIDENCE_PROCEED_MIN = 0.65          # >= 0.65 → proceed
_CONFIDENCE_FLAG_MIN = 0.40             # >= 0.40 → flag_for_review

# ── Pydantic schema ───────────────────────────────────────────────────────────


class ValidationResult(BaseModel):
    """Structured output of the validation node."""

    passed: bool = Field(
        ...,
        description="True if the code proposal passed all validation checks.",
    )
    file_existence_ok: bool = Field(
        ...,
        description="True if all (or an acceptable proportion of) proposed files exist.",
    )
    ac_coverage_ok: bool = Field(
        ...,
        description="True if the proposal appears to address all acceptance criteria.",
    )
    confidence_gate: str = Field(
        ...,
        description="'proceed', 'flag_for_review', or 'block'",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Hard blockers that caused a 'block' decision.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Soft issues that triggered 'flag_for_review'.",
    )
    recommendation: str = Field(
        ...,
        description="Short human-readable summary of the validation outcome.",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _determine_confidence_gate(
    confidence: float,
    hallucination_rate: float,
    issues: list[str],
) -> str:
    """
    Apply the confidence gate rules and return one of:
    ``"proceed"``, ``"flag_for_review"``, or ``"block"``.

    Hard blockers (entries already in *issues*) always result in ``"block"``.
    """
    if issues:
        return "block"

    if confidence >= _CONFIDENCE_PROCEED_MIN and hallucination_rate < _HALLUCINATION_FLAG_THRESHOLD:
        return "proceed"

    if confidence >= _CONFIDENCE_FLAG_MIN and hallucination_rate < _HALLUCINATION_BLOCK_THRESHOLD:
        return "flag_for_review"

    return "block"


def _build_recommendation(gate: str, issues: list[str], warnings: list[str]) -> str:
    """Generate a short human-readable recommendation string."""
    if gate == "proceed":
        return "Code proposal passed all validation checks and is ready for PR composition."
    if gate == "flag_for_review":
        w_summary = "; ".join(warnings[:3]) if warnings else "see warnings above"
        return (
            f"Code proposal has minor quality concerns ({w_summary}).  "
            "Flagged for human review before merging."
        )
    i_summary = "; ".join(issues[:3]) if issues else "see issues above"
    return (
        f"Code proposal blocked: {i_summary}.  "
        "The planner must revise the plan before proceeding."
    )


# ── Node function ─────────────────────────────────────────────────────────────


def validation_node(state: WorkflowState) -> dict:
    """
    LangGraph node — validates the code proposal before PR composition.

    Reads:
        state["code_proposal"]         — CodeProposal
        state["implementation_plan"]   — ImplementationPlan (for confidence)
        state["ticket_context"]        — TicketContext (for AC coverage check)

    Returns:
        {
            "validation_result": ValidationResult dict,
            # If gate == "block":
            "should_stop": True,
            "errors": [str, ...],
        }
    """
    code_proposal: CodeProposal | None = state.get("code_proposal")
    implementation_plan = state.get("implementation_plan")
    ticket_context = state.get("ticket_context")
    ticket_id = state.get("ticket_id", "unknown")
    run_id = state.get("run_id", "unknown")

    logger.info(
        "agent_node_entered",
        ticket_id=ticket_id,
        run_id=run_id,
        phase="final_validation",
    )

    issues: list[str] = []
    warnings: list[str] = []

    # ── Guard: no code proposal ───────────────────────────────────────────────
    if code_proposal is None or not code_proposal.file_changes:
        issues.append("No code proposal or no file changes were produced.")
        result = ValidationResult(
            passed=False,
            file_existence_ok=False,
            ac_coverage_ok=False,
            confidence_gate="block",
            issues=issues,
            warnings=warnings,
            recommendation=_build_recommendation("block", issues, warnings),
        )
        logger.warning(
            "validation_no_proposal",
            ticket_id=ticket_id,
            run_id=run_id,
        )
        return {
            "validation_result": result.model_dump(),
            "should_stop": True,
            "errors": [f"validation: {issues[0]}"],
        }

    # ── Check 1: Minimum one file ─────────────────────────────────────────────
    proposed_paths = [fc.file_path for fc in code_proposal.file_changes]
    if len(proposed_paths) == 0:
        issues.append("Code proposal contains zero file changes.")

    # ── Check 2: File existence via file index ────────────────────────────────
    # Only validate files that are being MODIFIED — new files (create) are
    # expected to not exist in the codebase and should not count as hallucinations.
    validation_data: dict[str, Any] = {}
    hallucination_rate = 0.0
    file_existence_ok = True

    new_file_paths = {
        fc.file_path.replace("\\", "/").lstrip("/")
        for fc in code_proposal.file_changes
        if fc.change_type == ChangeType.CREATE
    }
    paths_to_validate = [p for p in proposed_paths if p.replace("\\", "/").lstrip("/") not in new_file_paths]

    if paths_to_validate:
        validation_data = validate_proposed_paths(paths_to_validate)
        hallucination_rate = validation_data.get("hallucination_rate", 0.0)
        hallucinated = validation_data.get("hallucinated", [])

        if hallucination_rate > _HALLUCINATION_BLOCK_THRESHOLD:
            issues.append(
                f"Hallucination rate too high: {hallucination_rate:.0%} of modified files "
                f"do not exist in the codebase ({len(hallucinated)} files: "
                f"{', '.join(hallucinated[:5])})."
            )
            file_existence_ok = False
        elif hallucination_rate > 0:
            warnings.append(
                f"{hallucination_rate:.0%} of modified files could not be verified "
                f"({len(hallucinated)} path(s)): {', '.join(hallucinated[:3])}"
            )
    elif new_file_paths:
        # All proposed files are new creations — that's fine
        logger.info(
            "validation_all_new_files",
            ticket_id=ticket_id,
            new_files=len(new_file_paths),
        )

    # ── Check 3: Confidence score ─────────────────────────────────────────────
    confidence = 0.0
    if implementation_plan is not None:
        confidence = getattr(implementation_plan, "confidence_score", 0.0) or 0.0

    # Apply confidence adjustment from plan critique if available
    plan_critique = state.get("plan_critique") or {}
    confidence_adjustment = plan_critique.get("confidence_adjustment", 0.0) or 0.0
    adjusted_confidence = max(0.0, min(1.0, confidence + confidence_adjustment))

    if adjusted_confidence < _CONFIDENCE_FLAG_MIN:
        issues.append(
            f"Adjusted confidence score ({adjusted_confidence:.2f}) is below the minimum "
            f"threshold of {_CONFIDENCE_FLAG_MIN:.2f} required to proceed."
        )
    elif adjusted_confidence < _CONFIDENCE_PROCEED_MIN:
        warnings.append(
            f"Confidence score ({adjusted_confidence:.2f}) is below the 'proceed' "
            f"threshold ({_CONFIDENCE_PROCEED_MIN:.2f}) — flagging for human review."
        )

    # ── Check 4: AC coverage (heuristic) ─────────────────────────────────────
    ac_coverage_ok = True
    if ticket_context is not None:
        ac_text = getattr(ticket_context, "acceptance_criteria", None) or ""
        # Simple heuristic: if AC is present but proposal summary is very short,
        # flag a warning (a full NLI check would require an LLM call)
        if ac_text and len(code_proposal.summary) < 40:
            warnings.append(
                "Code proposal summary is very short — AC coverage could not be verified."
            )
            ac_coverage_ok = False

    # ── Scope check warning ───────────────────────────────────────────────────
    scope_check = state.get("scope_check") or {}
    if scope_check.get("warning"):
        warnings.append(f"Scope: {scope_check['warning']}")

    # ── Plan critique warnings ────────────────────────────────────────────────
    if plan_critique.get("ac_coverage_gaps"):
        gaps = plan_critique["ac_coverage_gaps"]
        warnings.append(
            f"Plan critic identified {len(gaps)} uncovered AC(s): "
            + "; ".join(str(g) for g in gaps[:3])
        )

    # ── Determine gate ────────────────────────────────────────────────────────
    gate = _determine_confidence_gate(adjusted_confidence, hallucination_rate, issues)
    passed = gate in ("proceed", "flag_for_review") and not issues

    result = ValidationResult(
        passed=passed,
        file_existence_ok=file_existence_ok,
        ac_coverage_ok=ac_coverage_ok,
        confidence_gate=gate,
        issues=issues,
        warnings=warnings,
        recommendation=_build_recommendation(gate, issues, warnings),
    )

    logger.info(
        "agent_node_completed",
        ticket_id=ticket_id,
        run_id=run_id,
        passed=passed,
        gate=gate,
        confidence=round(adjusted_confidence, 3),
        hallucination_rate=round(hallucination_rate, 3),
        issues_count=len(issues),
        warnings_count=len(warnings),
    )

    output: dict[str, Any] = {"validation_result": result.model_dump()}

    if gate == "block":
        output["should_stop"] = True
        output["errors"] = [f"validation: {issue}" for issue in issues]

    return output
