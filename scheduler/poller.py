# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agents.supervisor import run_workflow
from config.settings import settings
from app_logging.activity_logger import ActivityLogger
from mcp_client.client_factory import filter_jira_tools, get_mcp_client
from persistence.database import init_db
from persistence.repository import TicketRepository
from utils.mcp_helpers import find_tool, unwrap_tool_result

logger = ActivityLogger("poller")
_repo = TicketRepository()
_scheduler: BackgroundScheduler | None = None


# ── Jira polling ───────────────────────────────────────────────────────────────

async def _fetch_ready_ticket_ids() -> list[str]:
    """
    Query Jira via MCP for tickets in 'Ready for Dev' status.
    Returns ticket IDs that have not yet been processed.
    """
    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        search_tool = find_tool(jira_tools, "jira_search")
        if search_tool is None:
            logger.warning(
                "jira_search_tool_not_found",
                available_tools=[t.name for t in jira_tools],
            )
            return []

        result = await search_tool.ainvoke({
            "jql": settings.jira_poll_jql,
            "fields": "summary,status,assignee",
        })

    issues = unwrap_tool_result(result).get("issues", [])
    all_ids = [issue.get("key", "") for issue in issues if issue.get("key")]

    # Filter out already-processed tickets
    unprocessed = [tid for tid in all_ids if not _repo.is_ticket_processed(tid)]

    logger.info(
        "scheduler_poll_triggered",
        found_tickets=len(all_ids),
        unprocessed=len(unprocessed),
        ticket_ids=unprocessed,
    )
    return unprocessed


# ── Scheduler job ──────────────────────────────────────────────────────────────

def poll_and_trigger() -> None:
    """Synchronous APScheduler job: poll Jira and trigger workflows."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ticket_ids = loop.run_until_complete(_fetch_ready_ticket_ids())
    finally:
        loop.close()

    for ticket_id in ticket_ids:
        logger.info("scheduler_ticket_queued", ticket_id=ticket_id)
        try:
            final_state = run_workflow(ticket_id)
            logger.info(
                "scheduler_workflow_finished",
                ticket_id=ticket_id,
                phase=str(final_state.get("current_phase", "")),
                errors=final_state.get("errors", []),
            )
        except Exception as exc:
            logger.error("scheduler_workflow_error", exc=exc, ticket_id=ticket_id)


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    init_db()

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        poll_and_trigger,
        trigger=IntervalTrigger(seconds=settings.jira_poll_interval_seconds),
        id="jira_poll",
        max_instances=1,    # Prevent concurrent runs
        coalesce=True,      # Skip missed fires during downtime
        replace_existing=True,
    )
    _scheduler.start()

    logger.info(
        "scheduler_started",
        interval_seconds=settings.jira_poll_interval_seconds,
        jql=settings.jira_poll_jql,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")


def is_scheduler_running() -> bool:
    return _scheduler is not None and _scheduler.running


# ── Standalone entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    from config.logging_config import configure_logging
    configure_logging()

    logger.info("starting_poller_process")
    start_scheduler()

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_scheduler()
        logger.info("poller_process_stopped")
