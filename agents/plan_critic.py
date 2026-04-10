# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/plan_critic.py
======================
LLM-powered implementation plan reviewer.

The critic examines the planner's output and returns a structured verdict:
  * APPROVE  — plan is good enough to proceed to code proposal
  * REVISE   — plan has actionable issues that must be fixed first

Quality checks performed:
  1. Acceptance criterion coverage — every AC should map to at least one step.
  2. File hallucination — every file in affected_files must exist in the
     real file index.
  3. Over-engineering — flag plans with excessive steps relative to ticket
     complexity (bug_fix > 8 steps is almost certainly over-engineered).
  4. Vague / placeholder steps — "Update logic", "Refactor X", etc.
  5. Type mismatch — a bug_fix plan should not contain redesign steps.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.base_agent import BaseAgent
from schemas.plan import ImplementationPlan
from schemas.workflow_state import WorkflowState
from utils.file_index import get_file_index, validate_proposed_paths

# ── Pydantic output schema ────────────────────────────────────────────────────


class PlanCritiqueResult(BaseModel):
    """Structured result of the plan critic's review."""

    decision: str = Field(
        ...,
        description="'approve' or 'revise'",
    )
    overall_quality: str = Field(
        ...,
        description="'good', 'acceptable', or 'poor'",
    )
    is_over_engineered: bool = Field(
        ...,
        description="True if the number of steps is disproportionate to the ticket complexity.",
    )
    ac_coverage_gaps: list[str] = Field(
        default_factory=list,
        description="Acceptance criteria that are not addressed by any implementation step.",
    )
    hallucinated_files: list[str] = Field(
        default_factory=list,
        description="File paths listed in the plan that do not exist in the real codebase.",
    )
    unnecessary_steps: list[str] = Field(
        default_factory=list,
        description="Step titles that appear unnecessary, vague, or scope-creeping.",
    )
    feedback: str = Field(
        ...,
        description=(
            "2-3 sentences of concise, actionable feedback for the planner to revise the plan. "
            "Be specific about what to fix."
        ),
    )
    confidence_adjustment: float = Field(
        ...,
        ge=-1.0,
        le=0.1,
        description=(
            "Numeric adjustment to apply to the plan's confidence score. "
            "Negative for problems found, small positive for a clean plan."
        ),
    )


# ── Prompt templates ──────────────────────────────────────────────────────────

_CRITIC_SYSTEM = """\
You are a senior engineer reviewing an implementation plan before it proceeds
to code generation.  Be strict about over-engineering.

Rules of thumb:
- A bug fix should touch 1-3 files max and have no more than 5 steps.
- A simple UI change should touch 2-5 files and have no more than 6 steps.
- A feature can touch 3-8 files and have up to 10 steps.
- Refactor tickets should rarely introduce new files. Flag new files only if
  they appear to add features rather than extract existing code.
- Steps titled "Update logic", "Refactor X", "Clean up", or "Improve Y"
  with no concrete detail are considered vague and should be flagged.
- A bug_fix plan must not contain steps that redesign, re-architect, or
  refactor unrelated parts of the system.
"""

_CRITIC_HUMAN_TEMPLATE = """\
## Ticket
**ID**: {ticket_id}
**Type**: {ticket_type}
**Title**: {title}

**Acceptance Criteria**:
{acceptance_criteria}

---

## Implementation Plan to Review
**Summary**: {plan_summary}
**Confidence**: {confidence_score:.2f}
**Risk**: {risk_level}

### Steps ({step_count} total)
{steps_text}

### Affected files across all steps
{all_files_text}

---

## Pre-computed Facts (do NOT re-derive these — use them directly)
- Hallucinated files (not in real codebase): {hallucinated_files}
- File index size: {file_index_count} real files
- Steps with no concrete detail (heuristic): {vague_steps}
- Over-engineering threshold for {ticket_type}: >{oe_threshold} steps

## Your Task
Review the plan and produce a PlanCritiqueResult.  Use the pre-computed facts
above.  For ac_coverage_gaps, identify which acceptance criteria (if any) are
not addressed by any step title or description.  Approve only if the plan is
accurate, proportional, and covers all ACs.
"""

# Over-engineering thresholds per ticket type
_OE_THRESHOLDS: dict[str, int] = {
    "bug_fix": 5,
    "feature": 10,
    "ui_change": 6,
    "refactor": 7,
    "other": 8,
}

# Vague step title indicators
_VAGUE_INDICATORS = [
    "update logic",
    "refactor",
    "clean up",
    "cleanup",
    "improve",
    "adjust",
    "misc",
    "various",
    "other changes",
    "tbd",
    "todo",
    "placeholder",
]


def _detect_vague_steps(plan: ImplementationPlan, ticket_type: str = "other") -> list[str]:
    """Return step titles that appear vague or under-specified."""
    # For refactor tickets, "refactor" in a step title is expected — not vague
    active_indicators = [
        i for i in _VAGUE_INDICATORS
        if not (i == "refactor" and ticket_type == "refactor")
    ]
    vague: list[str] = []
    for step in plan.implementation_steps:
        title_lower = step.title.lower()
        desc_lower = step.description.lower()
        is_vague = any(indicator in title_lower for indicator in active_indicators)
        is_short_desc = len(step.description.strip()) < 50
        if is_vague or (is_short_desc and "fix" not in desc_lower):
            vague.append(f"Step {step.step_number}: {step.title}")
    return vague


def _collect_all_proposed_files(plan: ImplementationPlan) -> list[str]:
    """Flatten all affected_files across every implementation step."""
    seen: set[str] = set()
    files: list[str] = []
    for step in plan.implementation_steps:
        for f in step.affected_files:
            if f not in seen:
                seen.add(f)
                files.append(f)
    return files


# ── Agent ─────────────────────────────────────────────────────────────────────


class PlanCriticAgent(BaseAgent):
    """Reviews the implementation plan for quality issues using the LLM."""

    def run(self, state: WorkflowState) -> dict:
        implementation_plan: ImplementationPlan | None = state.get("implementation_plan")
        ticket_context = state.get("ticket_context")
        ticket_type = state.get("ticket_type") or "other"
        run_id = state.get("run_id", "unknown")
        ticket_id = state.get("ticket_id", "unknown")

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase="plan_critique",
        )

        if implementation_plan is None:
            self.logger.warning(
                "plan_critic_no_plan",
                ticket_id=ticket_id,
                run_id=run_id,
            )
            default_critique: dict[str, Any] = {
                "approved": False,
                "feedback": "No implementation plan was provided to review.",
                "revision_count": (state.get("plan_critique") or {}).get("revision_count", 0),
                "decision": "revise",
                "overall_quality": "poor",
                "is_over_engineered": False,
                "ac_coverage_gaps": [],
                "hallucinated_files": [],
                "unnecessary_steps": [],
                "confidence_adjustment": -0.3,
            }
            return {
                "plan_critique": default_critique,
                "llm_call_ids": [],
            }

        # ── Pre-compute hallucination data (deterministic) ───────────────────
        all_proposed_files = _collect_all_proposed_files(implementation_plan)
        validation = validate_proposed_paths(all_proposed_files)
        hallucinated_files = validation.get("hallucinated", [])

        # ── Detect vague steps (deterministic) ──────────────────────────────
        vague_steps = _detect_vague_steps(implementation_plan, ticket_type)

        # ── Over-engineering threshold ────────────────────────────────────────
        oe_threshold = _OE_THRESHOLDS.get(ticket_type, 8)
        step_count = len(implementation_plan.implementation_steps)

        # ── Build prompt ──────────────────────────────────────────────────────
        title = (
            ticket_context.title
            if ticket_context else "(unknown)"
        )
        acceptance_criteria = (
            getattr(ticket_context, "acceptance_criteria", None) or "(not provided)"
            if ticket_context else "(not provided)"
        )

        steps_text = "\n".join(
            f"  {s.step_number}. **{s.title}**: {s.description}\n"
            f"     Files: {', '.join(s.affected_files) or '(none listed)'}"
            for s in implementation_plan.implementation_steps
        )

        all_files_text = (
            "\n".join(f"  - {f}" for f in all_proposed_files)
            or "(no files listed)"
        )

        human_prompt = _CRITIC_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            ticket_type=ticket_type,
            title=title,
            acceptance_criteria=acceptance_criteria,
            plan_summary=implementation_plan.summary,
            confidence_score=implementation_plan.confidence_score,
            risk_level=implementation_plan.risk_level,
            step_count=step_count,
            steps_text=steps_text,
            all_files_text=all_files_text,
            hallucinated_files=(
                ", ".join(hallucinated_files) if hallucinated_files else "(none — all paths verified)"
            ),
            file_index_count=len(get_file_index()),
            vague_steps=(
                "; ".join(vague_steps) if vague_steps else "(none detected)"
            ),
            oe_threshold=oe_threshold,
        )

        try:
            result, call_id = self.invoke_llm_structured(
                system_prompt=_CRITIC_SYSTEM,
                human_prompt=human_prompt,
                output_schema=PlanCritiqueResult,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="plan_critique",
            )
        except Exception as exc:
            self.logger.error(
                "plan_critic_llm_failed",
                exc=exc,
                ticket_id=ticket_id,
                run_id=run_id,
            )
            fallback_critique: dict[str, Any] = {
                "approved": False,
                "feedback": f"Plan critic LLM call failed: {exc}",
                "revision_count": (state.get("plan_critique") or {}).get("revision_count", 0),
                "decision": "revise",
                "overall_quality": "poor",
                "is_over_engineered": step_count > oe_threshold,
                "ac_coverage_gaps": [],
                "hallucinated_files": hallucinated_files,
                "unnecessary_steps": vague_steps,
                "confidence_adjustment": -0.2,
            }
            return {
                "plan_critique": fallback_critique,
                "llm_call_ids": [],
                "errors": [f"plan_critic: {exc}"],
            }

        if result is None:
            self.logger.warning(
                "plan_critic_parse_none",
                ticket_id=ticket_id,
                run_id=run_id,
            )
            result_critique: dict[str, Any] = {
                "approved": False,
                "feedback": "Plan critique could not be parsed — treating as revise.",
                "revision_count": (state.get("plan_critique") or {}).get("revision_count", 0),
                "decision": "revise",
                "overall_quality": "poor",
                "is_over_engineered": False,
                "ac_coverage_gaps": [],
                "hallucinated_files": hallucinated_files,
                "unnecessary_steps": [],
                "confidence_adjustment": -0.1,
            }
            return {
                "plan_critique": result_critique,
                "llm_call_ids": [call_id] if call_id else [],
            }

        # Merge the structured LLM result with extra metadata
        prior_revision_count = (state.get("plan_critique") or {}).get("revision_count", 0)
        approved = result.decision.lower() == "approve"

        critique_dict: dict[str, Any] = {
            "approved": approved,
            "feedback": result.feedback,
            "revision_count": prior_revision_count,  # incremented by supervisor on REVISE loop
            "decision": result.decision.lower(),
            "overall_quality": result.overall_quality,
            "is_over_engineered": result.is_over_engineered,
            "ac_coverage_gaps": result.ac_coverage_gaps,
            "hallucinated_files": result.hallucinated_files or hallucinated_files,
            "unnecessary_steps": result.unnecessary_steps or vague_steps,
            "confidence_adjustment": result.confidence_adjustment,
        }

        self.logger.info(
            "agent_node_completed",
            ticket_id=ticket_id,
            run_id=run_id,
            decision=critique_dict["decision"],
            quality=critique_dict["overall_quality"],
            over_engineered=critique_dict["is_over_engineered"],
            hallucinated_count=len(critique_dict["hallucinated_files"]),
            ac_gaps=len(critique_dict["ac_coverage_gaps"]),
        )

        return {
            "plan_critique": critique_dict,
            "llm_call_ids": [call_id] if call_id else [],
            "total_llm_calls": state.get("total_llm_calls", 0) + 1,
        }


_agent = PlanCriticAgent()


def plan_critic_node(state: WorkflowState) -> dict:
    """LangGraph node entry point for the plan critic agent."""
    return _agent.run(state)
