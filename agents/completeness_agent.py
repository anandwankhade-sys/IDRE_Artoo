# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import json
from typing import Optional

from agents.base_agent import BaseAgent
from config.settings import settings
from app_logging.activity_logger import ActivityLogger
from mcp_client.client_factory import filter_jira_tools, get_mcp_client
from prompts.completeness_prompt import (
    COMPLETENESS_HUMAN_TEMPLATE,
    COMPLETENESS_SYSTEM,
)
from schemas.completeness import CompletenessDecision, CompletenessResult
from schemas.workflow_state import WorkflowPhase, WorkflowState

logger = ActivityLogger("completeness_agent")


async def _post_jira_comment(ticket_id: str, comment_body: str) -> Optional[str]:
    """Post clarification comment to Jira ticket. Returns comment ID or None."""
    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        add_comment_tool = next(
            (t for t in jira_tools if "comment" in t.name.lower() and "add" in t.name.lower()),
            None,
        ) or next(
            (t for t in jira_tools if "comment" in t.name.lower()),
            None,
        )

        if add_comment_tool is None:
            logger.warning(
                "jira_comment_tool_not_found",
                ticket_id=ticket_id,
                available_tools=[t.name for t in jira_tools],
            )
            return None

        result = await add_comment_tool.ainvoke(
            {"issue_key": ticket_id, "body": comment_body}
        )

        # langchain_mcp_adapters returns the MCP text content as a string, not a dict
        if isinstance(result, dict):
            return str(result.get("id", "")) or None

        if isinstance(result, str):
            logger.debug("jira_comment_raw_result", ticket_id=ticket_id, preview=result[:300])
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    return str(parsed.get("id", "")) or "posted"
            except (json.JSONDecodeError, ValueError):
                pass
            # Non-JSON string response — comment was posted but ID not extractable
            return "posted"

        return None


async def _apply_jira_label(ticket_id: str, label: str) -> None:
    """Add a label to the Jira ticket."""
    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        update_tool = next(
            (t for t in jira_tools if "update" in t.name.lower() and "issue" in t.name.lower()),
            None,
        )
        if update_tool is None:
            return

        await update_tool.ainvoke(
            {
                "issue_key": ticket_id,
                "fields": json.dumps({"labels": [label]}),
            }
        )


def _build_clarification_comment(result: CompletenessResult) -> str:
    lines = [
        "👋 **Artoo — Clarification Required**",
        "",
        f"This ticket has been reviewed and requires additional information before development can begin.",
        f"**Completeness score: {result.completeness_score:.0%}**",
        "",
    ]

    if result.missing_fields:
        lines.append("**Missing / insufficient fields:**")
        for mf in result.missing_fields:
            lines.append(f"- **{mf.field_name}** ({mf.severity}): {mf.description}")
        lines.append("")

    if result.clarification_questions:
        lines.append("**Please answer the following questions:**")
        for i, q in enumerate(result.clarification_questions, 1):
            lines.append(f"{i}. {q}")
        lines.append("")

    if result.assumptions_summary:
        lines.append(f"**Assumptions made:** {result.assumptions_summary}")
        lines.append("")

    lines.append(
        "_Once the above information is added, remove the 'Needs Clarification' label "
        "and transition the ticket back to 'Ready for Dev' to resume automated processing._"
    )
    return "\n".join(lines)


class CompletenessAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.CHECKING_COMPLETENESS,
        )

        if ticket_context is None:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["completeness_agent: ticket_context is None"],
                "should_stop": True,
            }

        human_prompt = COMPLETENESS_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=ticket_context.title,
            description=ticket_context.description or "(empty)",
            acceptance_criteria=ticket_context.acceptance_criteria or "(not provided)",
            labels=", ".join(ticket_context.labels) or "(none)",
            priority=ticket_context.priority or "(not set)",
            story_points=ticket_context.story_points or "(not set)",
            attachments=(
                ", ".join(a.filename for a in ticket_context.attachments)
                if ticket_context.attachments
                else "(none)"
            ),
            linked_issues=", ".join(ticket_context.linked_issues) or "(none)",
        )

        try:
            result, call_id = self.invoke_llm_structured(
                system_prompt=COMPLETENESS_SYSTEM,
                human_prompt=human_prompt,
                output_schema=CompletenessResult,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="completeness_evaluation",
            )

            if result is None:
                raise ValueError("LLM returned None for CompletenessResult")

            # Ensure ticket_id is set (LLM may omit it)
            result.ticket_id = ticket_id

            # Floor: a ticket with a title should never be 0.0
            if result.completeness_score == 0.0 and ticket_context.title:
                result.completeness_score = 0.15

            # Score-only gate: if the score meets the threshold, proceed
            # regardless of the LLM's decision field (the decision was
            # blocking tickets that had enough info to attempt)
            is_complete = result.completeness_score >= settings.completeness_threshold

            # Bug-type tickets with a descriptive title get extra leniency —
            # a clear symptom description is enough for experienced codebases
            ticket_type = state.get("ticket_type", "other")
            if ticket_type == "bug_fix" and ticket_context.title:
                is_complete = True

            self.logger.info(
                "completeness_evaluated",
                ticket_id=ticket_id,
                run_id=run_id,
                score=result.completeness_score,
                decision=result.decision,
                is_complete=is_complete,
            )

            return {
                "completeness_result": result,
                "is_complete_ticket": is_complete,
                "current_phase": WorkflowPhase.CHECKING_COMPLETENESS,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"completeness_agent: {exc}"],
                "should_stop": True,
            }


class PostClarificationAgent(BaseAgent):
    """Posts a clarification comment on Jira and labels the ticket."""

    def run(self, state: WorkflowState) -> dict:
        completeness_result = state.get("completeness_result")
        ticket_id = state["ticket_id"]
        run_id = state["run_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.POSTING_CLARIFICATION,
        )

        if completeness_result is None:
            return {
                "current_phase": WorkflowPhase.POSTING_CLARIFICATION,
                "errors": ["post_clarification: completeness_result is None"],
            }

        if settings.dry_run or settings.jira_read_only:
            self.logger.info(
                "jira_write_suppressed",
                ticket_id=ticket_id,
                run_id=run_id,
                reason="dry_run" if settings.dry_run else "jira_read_only",
            )
            return {"current_phase": WorkflowPhase.COMPLETED}

        try:
            comment_body = _build_clarification_comment(completeness_result)
            comment_id = self.run_async(_post_jira_comment(ticket_id, comment_body))

            if comment_id:
                completeness_result.jira_comment_posted = True
                completeness_result.jira_comment_id = comment_id
                self.logger.info(
                    "jira_comment_posted",
                    ticket_id=ticket_id,
                    run_id=run_id,
                    comment_id=comment_id,
                )

            # Apply "needs-clarification" label
            self.run_async(_apply_jira_label(ticket_id, "needs-clarification"))
            self.logger.info("jira_label_applied", ticket_id=ticket_id, label="needs-clarification")

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.POSTING_CLARIFICATION,
                "errors": [f"post_clarification: {exc}"],
                "completeness_result": completeness_result,
            }

        return {
            "completeness_result": completeness_result,
            "current_phase": WorkflowPhase.COMPLETED,
            "mcp_tool_calls": [{"tool": "jira_add_comment", "ticket_id": ticket_id}],
        }


# ── LangGraph node functions ───────────────────────────────────────────────────

_completeness_agent = CompletenessAgent()
_clarification_agent = PostClarificationAgent()


def completeness_check_node(state: WorkflowState) -> dict:
    return _completeness_agent.run(state)


def post_clarification_node(state: WorkflowState) -> dict:
    return _clarification_agent.run(state)
