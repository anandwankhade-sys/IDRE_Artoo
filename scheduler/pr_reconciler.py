# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio

from app_logging.activity_logger import ActivityLogger
from mcp_client.client_factory import filter_github_tools, get_mcp_client
from persistence.models import PROutcome
from persistence.repository import TicketRepository

logger = ActivityLogger("pr_reconciler")
_repo = TicketRepository()


async def _check_pr_state(
    tools: list,
    owner: str,
    repo: str,
    pr_number: int,
) -> PROutcome:
    """Call GitHub MCP to get current PR state and return outcome enum."""
    get_pr_tool = next(
        (t for t in tools if "get_pull_request" in t.name.lower()),
        None,
    )
    if get_pr_tool is None:
        logger.warning("github_get_pr_tool_not_found")
        return PROutcome.PENDING

    try:
        pr_data = await get_pr_tool.ainvoke({
            "owner": owner,
            "repo": repo,
            "pullNumber": pr_number,
        })
        if not isinstance(pr_data, dict):
            return PROutcome.PENDING

        merged = pr_data.get("merged", False)
        state = pr_data.get("state", "open")
        reviews = pr_data.get("reviews", []) or []

        if merged:
            return PROutcome.MERGED
        if state == "closed":
            return PROutcome.REJECTED

        # Check for approval reviews
        if any(r.get("state") == "APPROVED" for r in reviews):
            return PROutcome.APPROVED

        return PROutcome.PENDING

    except Exception as exc:
        logger.warning("github_pr_check_failed", pr_number=pr_number, error=str(exc))
        return PROutcome.PENDING


async def _reconcile_all() -> None:
    from config.settings import settings

    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    pending_runs = _repo.get_pending_pr_runs()
    if not pending_runs:
        logger.info("pr_reconciler_nothing_to_reconcile")
        return

    logger.info("pr_reconciler_started", pending_count=len(pending_runs))

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        gh_tools = filter_github_tools(all_tools)

        for run in pending_runs:
            if not run.pr_number:
                continue

            outcome = await _check_pr_state(gh_tools, owner, repo, run.pr_number)
            if outcome != PROutcome.PENDING:
                _repo.set_pr_outcome(run.id, outcome)
                logger.info(
                    "pr_outcome_updated",
                    run_id=run.id,
                    ticket_id=run.ticket_id,
                    pr_number=run.pr_number,
                    outcome=outcome.value,
                )


def reconcile_pr_outcomes() -> None:
    """Synchronous entry point called by APScheduler."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_reconcile_all())
    except Exception as exc:
        logger.error("pr_reconciler_failed", exc=exc)
    finally:
        loop.close()
