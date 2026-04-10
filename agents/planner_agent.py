# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from agents.base_agent import BaseAgent
from prompts.planner_prompt import (
    PLANNER_HUMAN_TEMPLATE,
    PLANNER_SYSTEM,
    HYBRID_PLANNER_HUMAN_TEMPLATE,
    HYBRID_PLANNER_SYSTEM,
)
from schemas.plan import ImplementationPlan
from schemas.workflow_state import WorkflowPhase, WorkflowState


class PlannerAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        repo_context = state.get("repo_context")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.PLANNING,
        )

        if ticket_context is None:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["planner: missing ticket_context"],
                "should_stop": True,
            }

        # ── Decide: hybrid or legacy prompt ──────────────────────────────────
        assembled_context = state.get("assembled_context")
        exploration_context = state.get("exploration_context")
        use_hybrid = assembled_context is not None

        if use_hybrid:
            system_prompt, human_prompt = self._build_hybrid_prompt(
                state, ticket_context, assembled_context, exploration_context
            )
        else:
            system_prompt, human_prompt = self._build_legacy_prompt(
                state, ticket_context, repo_context
            )

        try:
            result, call_id = self.invoke_llm_structured(
                system_prompt=system_prompt,
                human_prompt=human_prompt,
                output_schema=ImplementationPlan,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="implementation_planning",
            )

            if result is None:
                raise ValueError("LLM returned None for ImplementationPlan")

            result.ticket_id = ticket_id

            self.logger.info(
                "agent_node_completed",
                ticket_id=ticket_id,
                run_id=run_id,
                steps=len(result.implementation_steps),
                risk_level=result.risk_level,
                confidence=result.confidence_score,
                hybrid=use_hybrid,
            )

            return {
                "implementation_plan": result,
                "current_phase": WorkflowPhase.PROPOSING_CODE,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"planner: {exc}"],
                "should_stop": True,
            }

    def _build_hybrid_prompt(
        self, state, ticket_context, assembled_context, exploration_context
    ) -> tuple[str, str]:
        """Build prompt using structured code intelligence context."""
        ticket_id = state["ticket_id"]
        ticket_type = state.get("ticket_type") or "other"

        # Scope baseline info
        scope_baseline = assembled_context.get("scope_baseline", {})
        avg = scope_baseline.get("avg_files", "?")
        p75 = scope_baseline.get("p75_files", "?")
        scope_info = (
            f"Ticket type: {ticket_type}\n"
            f"Similar tickets average {avg} files changed (75th percentile: {p75}).\n"
            f"Keep your plan within this range unless clearly justified."
        )

        # Existing tests (from repo scout, if available)
        repo_context = state.get("repo_context")
        existing_tests = "(none found)"
        if repo_context and getattr(repo_context, "existing_test_files", None):
            existing_tests = "\n".join(f"- {t}" for t in repo_context.existing_test_files[:10])

        # Confluence context
        confluence_context = state.get("confluence_context")
        confluence_summary = "(not available)"
        if confluence_context:
            confluence_summary = getattr(confluence_context, "summary", "(not available)") or "(not available)"

        # Revision feedback (if this is a re-plan after critique)
        revision_feedback = ""
        plan_critique = state.get("plan_critique")
        revision_count = state.get("plan_revision_count", 0)
        if revision_count > 0 and plan_critique:
            feedback_text = plan_critique.get("feedback", "")
            hallucinated = plan_critique.get("hallucinated_files", [])
            unnecessary = plan_critique.get("unnecessary_steps", [])
            revision_feedback = (
                f"## ⚠ REVISION REQUIRED (attempt {revision_count + 1})\n"
                f"Previous plan was rejected. Feedback:\n{feedback_text}\n"
            )
            if hallucinated:
                revision_feedback += f"Hallucinated files to remove: {', '.join(hallucinated)}\n"
            if unnecessary:
                revision_feedback += f"Unnecessary steps to remove: {', '.join(unnecessary)}\n"
            revision_feedback += "Please address ALL issues above in your revised plan.\n"

        human_prompt = HYBRID_PLANNER_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=ticket_context.title,
            ticket_type=ticket_type,
            description=ticket_context.description or "(empty)",
            acceptance_criteria=ticket_context.acceptance_criteria or "(not provided)",
            scope_baseline_info=scope_info,
            confluence_summary=confluence_summary,
            co_change_hints=assembled_context.get("co_change_hints", "(not available)"),
            assembled_summaries=assembled_context.get("file_summaries_section", "(not available)"),
            assembled_repo_map=assembled_context.get("repo_map_section", "(not available)"),
            existing_tests=existing_tests,
            exploration_context=exploration_context or "(not available)",
            revision_feedback=revision_feedback,
        )

        return HYBRID_PLANNER_SYSTEM, human_prompt

    def _build_legacy_prompt(self, state, ticket_context, repo_context) -> tuple[str, str]:
        """Build prompt using the original RAG-based approach (fallback)."""
        ticket_id = state["ticket_id"]

        if repo_context is None:
            return PLANNER_SYSTEM, f"Ticket: {ticket_id}\nTitle: {ticket_context.title}\n(no repo context)"

        relevant_files_text = "\n".join(
            f"- {f.file_path} (relevance: {f.relevance_score:.2f}): {f.relevance_reason}"
            for f in repo_context.relevant_files[:15]
        )

        confluence_context = state.get("confluence_context")
        confluence_summary = (
            confluence_context.summary
            if confluence_context and confluence_context.summary
            else "(not available)"
        )
        confluence_pages_text = (
            "\n".join(
                f"- [{p.title}]({p.url}) — {p.relevance_reason}"
                for p in confluence_context.pages_found
            )
            if confluence_context and confluence_context.pages_found
            else "(none retrieved)"
        )

        human_prompt = PLANNER_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=ticket_context.title,
            description=ticket_context.description or "(empty)",
            acceptance_criteria=ticket_context.acceptance_criteria or "(not provided)",
            primary_language=repo_context.primary_language or "Unknown",
            impacted_modules=", ".join(repo_context.impacted_modules) or "(unknown)",
            relevant_files=relevant_files_text or "(none identified)",
            code_style_hints=repo_context.code_style_hints or "(not detected)",
            dependency_hints="\n".join(f"- {d}" for d in repo_context.dependency_hints[:10]),
            existing_tests=(
                "\n".join(f"- {t}" for t in repo_context.existing_test_files[:10])
                or "(none found)"
            ),
            confluence_summary=confluence_summary,
            confluence_pages=confluence_pages_text,
        )

        return PLANNER_SYSTEM, human_prompt


_agent = PlannerAgent()


def planner_node(state: WorkflowState) -> dict:
    return _agent.run(state)
