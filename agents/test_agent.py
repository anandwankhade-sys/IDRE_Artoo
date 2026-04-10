# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from agents.base_agent import BaseAgent
from prompts.test_prompt import TEST_HUMAN_TEMPLATE, TEST_SYSTEM
from schemas.test_suggestion import TestSuggestions
from schemas.workflow_state import WorkflowPhase, WorkflowState


class TestAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        implementation_plan = state.get("implementation_plan")
        code_proposal = state.get("code_proposal")
        repo_context = state.get("repo_context")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.SUGGESTING_TESTS,
        )

        if not ticket_context or not implementation_plan:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["test_agent: missing required state"],
                "should_stop": True,
            }

        changed_files = []
        code_changes_summary = []
        if code_proposal:
            for fc in code_proposal.file_changes:
                changed_files.append(fc.file_path)
                code_changes_summary.append(f"- {fc.file_path} ({fc.change_type.value}): {fc.rationale}")

        human_prompt = TEST_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=ticket_context.title,
            description=ticket_context.description or "(empty)",
            acceptance_criteria=ticket_context.acceptance_criteria or "(not provided)",
            ticket_type=state.get("ticket_type") or "other",
            plan_summary=implementation_plan.summary,
            changed_files=", ".join(changed_files) or "(none)",
            code_changes_summary="\n".join(code_changes_summary) or "(no code changes)",
            existing_tests=(
                "\n".join(f"- {t}" for t in (repo_context.existing_test_files[:10] if repo_context else []))
                or "(none found)"
            ),
        )

        try:
            result, call_id = self.invoke_llm_structured(
                system_prompt=TEST_SYSTEM,
                human_prompt=human_prompt,
                output_schema=TestSuggestions,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="test_suggestion_generation",
            )

            if result is None:
                raise ValueError("LLM returned None for TestSuggestions")

            result.ticket_id = ticket_id

            self.logger.info(
                "agent_node_completed",
                ticket_id=ticket_id,
                run_id=run_id,
                test_cases=len(result.test_cases),
                confidence=result.confidence_score,
            )

            return {
                "test_suggestions": result,
                "current_phase": WorkflowPhase.COMPOSING_PR,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"test_agent: {exc}"],
                "should_stop": True,
            }


_agent = TestAgent()


def test_suggestion_node(state: WorkflowState) -> dict:
    return _agent.run(state)
