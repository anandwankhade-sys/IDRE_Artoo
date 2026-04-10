# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from agents.base_agent import BaseAgent
from agents.context_assembler import _truncate_at_boundary
from prompts.code_proposal_prompt import CODE_PROPOSAL_SYSTEM, CODE_PROPOSAL_HUMAN_TEMPLATE
from schemas.code_proposal import CodeProposal
from schemas.workflow_state import WorkflowPhase, WorkflowState
from utils.markdown_parser import parse_markdown_code_proposal


class CodeProposalAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        repo_context = state.get("repo_context")
        implementation_plan = state.get("implementation_plan")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.PROPOSING_CODE,
        )

        if not ticket_context or not implementation_plan:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["code_proposal: missing required state"],
                "should_stop": True,
            }

        plan_steps_text = "\n".join(
            f"{s.step_number}. {s.title}: {s.description} (files: {', '.join(s.affected_files)})"
            for s in implementation_plan.implementation_steps
        )

        # Build targeted relevant files list:
        # 1. All files mentioned in the implementation plan steps (highest priority)
        # 2. Files from repo_context.relevant_files (RepoScout KB selection)
        # Capped at ~40 files to stay within prompt budget
        plan_files: list[str] = []
        for step in implementation_plan.implementation_steps:
            plan_files.extend(step.affected_files)

        repo_files: list[str] = []
        if repo_context and repo_context.relevant_files:
            repo_files = [f.file_path for f in repo_context.relevant_files]

        # Add co-change partners for plan files — files that historically
        # change together should be included in the proposal scope
        assembled_ctx = state.get("assembled_context") or {}
        co_change_hints = assembled_ctx.get("co_change_hints", "")
        co_change_files: list[str] = []
        if co_change_hints:
            # Parse co_change_hints text to extract file paths
            for line in co_change_hints.split("\n"):
                # Format: "  file_a + file_b  (N commits)"
                if "+" in line and "commit" in line.lower():
                    parts = line.split("+")
                    for part in parts:
                        path = part.strip().split("(")[0].strip().lstrip("- ")
                        if path and "/" in path:
                            co_change_files.append(path)

        # Merge, de-duplicate, plan files first, then co-change partners
        seen: set[str] = set()
        targeted_paths: list[str] = []
        for p in plan_files + co_change_files + repo_files:
            norm = p.replace("\\", "/").lstrip("/")
            if norm and norm not in seen:
                seen.add(norm)
                targeted_paths.append(norm)
        targeted_paths = targeted_paths[:40]

        relevant_files_text = "\n".join(targeted_paths) or "(no specific files identified)"

        # Use exploration_context as code snippets — it contains actual source code
        # fetched by ExplorerAgent (up to 80 lines per file, 3-5 files)
        exploration_context = state.get("exploration_context") or ""
        if exploration_context and len(exploration_context) > 200:
            code_snippets = _truncate_at_boundary(exploration_context, 8000)
        elif repo_context and repo_context.relevant_files:
            # Fallback: metadata summary if no exploration context
            snippets = []
            for f in repo_context.relevant_files[:8]:
                snippet = f"**{f.file_path}**"
                if f.functions_detected:
                    snippet += f" — functions: {', '.join(f.functions_detected[:5])}"
                if f.classes_detected:
                    snippet += f" — classes: {', '.join(f.classes_detected[:3])}"
                snippets.append(snippet)
            code_snippets = "\n".join(snippets)
        else:
            code_snippets = "(no code exploration available)"

        human_prompt = CODE_PROPOSAL_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=ticket_context.title,
            description=ticket_context.description or "(empty)",
            acceptance_criteria=ticket_context.acceptance_criteria or "(not provided)",
            plan_summary=implementation_plan.summary,
            plan_steps=plan_steps_text,
            ticket_type=state.get("ticket_type") or "other",
            relevant_files=relevant_files_text,
            relevant_file_count=len(targeted_paths),
            code_snippets=code_snippets,
            co_change_hints=co_change_hints[:2000] if co_change_hints else "(none)",
            code_style_hints=(
                repo_context.code_style_hints if repo_context else "Unknown"
            ) or "Unknown",
        )

        try:
            # Use regular LLM invoke (not structured) and parse markdown response
            markdown_response, call_id = self.invoke_llm(
                system_prompt=CODE_PROPOSAL_SYSTEM,
                human_prompt=human_prompt,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="code_proposal_generation",
            )

            if not markdown_response or not markdown_response.strip():
                self.logger.warning(
                    "agent_node_skipped_empty_response",
                    ticket_id=ticket_id,
                    run_id=run_id,
                    reason="LLM returned empty response",
                )
                return {
                    "code_proposal": None,
                    "current_phase": WorkflowPhase.SUGGESTING_TESTS,
                    "llm_call_ids": [call_id] if call_id else [],
                    "total_llm_calls": state.get("total_llm_calls", 0) + 1,
                    "errors": ["code_proposal: LLM returned empty response (skipped)"],
                }

            # Parse markdown response into CodeProposal object
            result = parse_markdown_code_proposal(
                markdown_text=markdown_response,
                ticket_id=ticket_id,
            )

            if result is None:
                # Markdown parser could not extract a valid CodeProposal
                self.logger.warning(
                    "agent_node_skipped_no_parse",
                    ticket_id=ticket_id,
                    run_id=run_id,
                    reason="Markdown parser could not extract CodeProposal from LLM response",
                )
                return {
                    "code_proposal": None,
                    "current_phase": WorkflowPhase.SUGGESTING_TESTS,
                    "llm_call_ids": [call_id] if call_id else [],
                    "total_llm_calls": state.get("total_llm_calls", 0) + 1,
                    "errors": ["code_proposal: Markdown parse failed (skipped)"],
                }

            result.ticket_id = ticket_id

            self.logger.info(
                "agent_node_completed",
                ticket_id=ticket_id,
                run_id=run_id,
                file_changes=len(result.file_changes),
                confidence=result.confidence_score,
            )

            return {
                "code_proposal": result,
                "current_phase": WorkflowPhase.SUGGESTING_TESTS,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"code_proposal: {exc}"],
                "should_stop": True,
            }


_agent = CodeProposalAgent()


def code_proposal_node(state: WorkflowState) -> dict:
    return _agent.run(state)
