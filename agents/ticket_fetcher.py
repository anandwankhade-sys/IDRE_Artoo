# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from agents.base_agent import BaseAgent
from app_logging.activity_logger import ActivityLogger
from mcp_client.client_factory import filter_jira_tools, get_mcp_client
from schemas.ticket import TicketContext
from schemas.workflow_state import WorkflowPhase, WorkflowState
from utils.mcp_helpers import find_tool, unwrap_tool_result as _unwrap_tool_result
from utils.sanitizer import redact_pii

logger = ActivityLogger("ticket_fetcher")

# ── Local cache directories (checked in order) ─────────────────────────────
_HYBRID_ROOT = Path(__file__).parent.parent
_CACHE_DIRS = [
    _HYBRID_ROOT / "jira_cache",
    _HYBRID_ROOT / "multi_model_results" / "jiras",
]



def _load_from_cache(ticket_id: str) -> dict | None:
    """Try to load a ticket from local JSON cache files."""
    for cache_dir in _CACHE_DIRS:
        cache_file = cache_dir / f"{ticket_id}.json"
        if cache_file.exists():
            try:
                with open(cache_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                logger.info("ticket_loaded_from_cache", ticket_id=ticket_id, path=str(cache_file))
                return data
            except Exception as exc:
                logger.warning("cache_load_failed", ticket_id=ticket_id, error=str(exc))
    return None


async def _fetch_ticket_via_mcp(ticket_id: str) -> dict:
    """Use the Jira MCP to fetch a single issue."""
    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        # Find the get_issue tool — use exact name to avoid matching jira_get_issue_watchers
        exact_match = next((t for t in jira_tools if t.name == "jira_get_issue"), None)
        get_issue_tool = exact_match or find_tool(jira_tools, "jira_get_issue") or find_tool(jira_tools, "get_jira")
        if get_issue_tool is None:
            # Fallback: try any search tool with exact issue key
            search_tool = find_tool(jira_tools, "search")
            if search_tool is None:
                raise RuntimeError(
                    f"No suitable Jira MCP tool found. Available: {[t.name for t in jira_tools]}"
                )
            result = await search_tool.ainvoke({"jql": f'issue = "{ticket_id}"', "max_results": 1})
            issues = _unwrap_tool_result(result).get("issues", [])
            if not issues:
                raise ValueError(f"Ticket {ticket_id} not found via JQL search")
            return issues[0]

        result = await get_issue_tool.ainvoke({"issue_key": ticket_id})
        return _unwrap_tool_result(result)


def _parse_jira_response(data: dict, ticket_id: str) -> TicketContext:
    """Convert raw Jira MCP response dict to TicketContext."""
    fields = data.get("fields", data)  # mcp-atlassian may return fields directly

    description = fields.get("description", "") or ""
    # Jira description can be Atlassian Document Format (ADF) or plain text
    if isinstance(description, dict):
        description = _flatten_adf(description)
    description = redact_pii(description)

    # Acceptance criteria may be stored in a custom field or extracted from description
    ac = fields.get("acceptance_criteria") or fields.get("customfield_10016") or ""
    if isinstance(ac, dict):
        ac = _flatten_adf(ac)
    ac = redact_pii(ac)

    # If no dedicated AC field, attempt to extract the "Acceptance Criteria" section from
    # the markdown description (e.g. "## Acceptance Criteria\n- ...").
    if not ac and description:
        import re
        ac_match = re.search(
            r"#+\s*Acceptance Criteria\s*\n(.*?)(?=\n#+\s|\Z)",
            description,
            re.IGNORECASE | re.DOTALL,
        )
        if ac_match:
            ac = ac_match.group(1).strip()

    labels = fields.get("labels", []) or []
    components = [c.get("name", "") for c in (fields.get("components") or [])]
    linked = [
        link.get("inwardIssue", {}).get("key", "") or link.get("outwardIssue", {}).get("key", "")
        for link in (fields.get("issuelinks") or [])
    ]

    priority_obj = fields.get("priority") or {}
    assignee_obj = fields.get("assignee") or {}
    reporter_obj = fields.get("reporter") or {}

    # Extract attachments
    attachments = []
    for att in fields.get("attachment") or []:
        if isinstance(att, dict):
            from schemas.ticket import JiraAttachment
            attachments.append(JiraAttachment(
                filename=att.get("filename", ""),
                content_type=att.get("mimeType", att.get("content_type", "")),
                size_bytes=att.get("size", 0),
                url=att.get("content", att.get("url", "")),
            ))

    return TicketContext(
        ticket_id=ticket_id,
        title=fields.get("summary", ""),
        description=description,
        acceptance_criteria=ac or None,
        labels=labels,
        priority=priority_obj.get("name") if isinstance(priority_obj, dict) else str(priority_obj),
        story_points=fields.get("story_points") or fields.get("customfield_10028"),
        reporter=reporter_obj.get("displayName") if isinstance(reporter_obj, dict) else None,
        assignee=assignee_obj.get("displayName") if isinstance(assignee_obj, dict) else None,
        status=str(fields.get("status", {}).get("name", "")) if isinstance(fields.get("status"), dict) else "",
        attachments=attachments,
        components=components,
        linked_issues=[k for k in linked if k],
        raw_jira_data=data,
    )


def _flatten_adf(adf: Any) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if isinstance(adf, str):
        return adf
    if isinstance(adf, dict):
        texts = []
        if adf.get("type") == "text":
            texts.append(adf.get("text", ""))
        for child in adf.get("content", []):
            texts.append(_flatten_adf(child))
        return " ".join(t for t in texts if t).strip()
    if isinstance(adf, list):
        return " ".join(_flatten_adf(item) for item in adf)
    return str(adf)


class TicketFetcherAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_id = state["ticket_id"]
        run_id = state["run_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.FETCHING_TICKET,
        )

        try:
            # Try local cache first (avoids MCP / network dependency)
            cached = _load_from_cache(ticket_id)
            if cached is not None:
                raw_data = cached
                source = "cache"
            else:
                raw_data = self.run_async(_fetch_ticket_via_mcp(ticket_id))
                source = "mcp"

            ticket_context = _parse_jira_response(raw_data, ticket_id)

            self.logger.info(
                "jira_ticket_fetched",
                ticket_id=ticket_id,
                run_id=run_id,
                title=ticket_context.title,
                source=source,
            )

            return {
                "ticket_context": ticket_context,
                "current_phase": WorkflowPhase.CHECKING_COMPLETENESS,
                "mcp_tool_calls": [{"tool": "jira_get_issue", "ticket_id": ticket_id, "source": source}],
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"ticket_fetcher: {exc}"],
                "should_stop": True,
            }


# ── LangGraph node function ────────────────────────────────────────────────────

_agent = TicketFetcherAgent()


def fetch_ticket_node(state: WorkflowState) -> dict:
    return _agent.run(state)
