# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import re
from typing import Optional

from agents.base_agent import BaseAgent
from app_logging.activity_logger import ActivityLogger
from config.settings import settings
from mcp_client.client_factory import filter_confluence_tools, get_mcp_client
from prompts.confluence_prompt import CONFLUENCE_HUMAN_TEMPLATE, CONFLUENCE_SYSTEM
from schemas.confluence import ConfluenceContext, ConfluencePage
from schemas.workflow_state import WorkflowPhase, WorkflowState
from utils.mcp_helpers import find_tool

logger = ActivityLogger("confluence_agent")

# ── Query generation ──────────────────────────────────────────────────────────

_QUERY_STOPWORDS = {
    # Articles / prepositions / conjunctions
    "the", "and", "for", "not", "are", "has", "had", "its", "our", "all",
    "any", "each", "was", "were", "but", "yet", "nor", "can", "may", "via",
    "under", "over", "about", "after", "before", "between", "within", "across", "out",
    # Common verbs
    "should", "will", "need", "must", "want", "have", "been", "able",
    "make", "ensure", "using", "used", "include", "includes", "currently",
    "create", "update", "change", "remove", "add", "get", "set", "show",
    "improve", "implement", "allow", "display", "return", "provide", "support",
    "handle", "build", "move", "click", "view", "open", "close", "send",
    # Filler
    "with", "from", "that", "this", "when", "into", "also", "some", "more",
    "than", "then", "they", "them", "their", "please", "existing",
    # Jira-speak
    "user", "users", "ticket", "issue", "task", "story", "epic",
    "done", "todo", "jira", "feature",
}


def _build_confluence_queries(ticket_context) -> list[str]:
    """
    Build 5 targeted Confluence search queries from ticket context.

    Strategy (in priority order):
    1. Full ticket title — most specific phrase match
    2. Meaningful bigrams from description — domain-level phrases
    3. Key terms from acceptance criteria — actionable specifics
    4. Domain entity nouns from full text — business objects (e.g. "payment", "invoice")
    5. Ticket type + primary domain noun — broadens scope if narrow queries return nothing
    """
    title = getattr(ticket_context, "title", "") or ""
    desc = getattr(ticket_context, "description", "") or ""
    ac = getattr(ticket_context, "acceptance_criteria", "") or ""

    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    # 1. Full title as a phrase (Confluence full-text search handles this well)
    if title:
        _add(title)

    # 2. Bigrams from description — meaningful two-word phrases
    desc_words = [w.lower() for w in re.findall(r"\b[a-zA-Z][a-zA-Z]{2,}\b", desc)
                  if w.lower() not in _QUERY_STOPWORDS]
    for i in range(len(desc_words) - 1):
        bigram = f"{desc_words[i]} {desc_words[i+1]}"
        _add(bigram)
        if len(queries) >= 3:
            break

    # 3. Meaningful single terms from acceptance criteria
    ac_words = [w.lower() for w in re.findall(r"\b[a-zA-Z][a-zA-Z]{3,}\b", ac)
                if w.lower() not in _QUERY_STOPWORDS]
    for w in dict.fromkeys(ac_words):  # preserve order, deduplicate
        _add(w)
        if len(queries) >= 4:
            break

    # 4. Domain entity nouns from full text — capitalised proper nouns or known domain terms
    full = f"{title} {desc} {ac}"
    proper_nouns = re.findall(r"\b[A-Z][a-z]{3,}\b", full)
    for noun in dict.fromkeys(proper_nouns):
        if noun.lower() not in _QUERY_STOPWORDS:
            _add(noun)
        if len(queries) >= 5:
            break

    # 5. Fallback: first 3 meaningful words from title
    title_words = [w.lower() for w in re.findall(r"\b[a-zA-Z][a-zA-Z]{3,}\b", title)
                   if w.lower() not in _QUERY_STOPWORDS]
    for w in title_words[:3]:
        _add(w)
        if len(queries) >= 5:
            break

    return queries[:5]


# ── Content cleaning ──────────────────────────────────────────────────────────

def _strip_html(raw: str, max_chars: int = 1500) -> str:
    """
    Strip Confluence storage-format XML/HTML tags and return clean plain text.
    Truncates to max_chars of actual readable content.
    """
    # Remove XML/HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    # Remove structured macro boilerplate and CDATA
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", " ", text, flags=re.DOTALL)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


# ── MCP helper functions ──────────────────────────────────────────────────────


async def _search_confluence(tools: list, space_keys: list[str], query: str) -> list[dict]:
    """Search Confluence for pages matching the query."""
    search_tool = find_tool(tools, "confluence_search") or find_tool(tools, "search")
    if search_tool is None:
        return []

    try:
        import json as _json
        params: dict = {"query": query, "limit": 10}
        # Restrict search to configured spaces
        if space_keys:
            params["spaces_filter"] = ",".join(space_keys)
        result = await search_tool.ainvoke(params)

        # mcp-atlassian returns a list of content items where the actual page
        # data is a JSON array stored in item["text"].  Parse and flatten.
        if isinstance(result, list):
            pages: list[dict] = []
            for item in result:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        parsed = _json.loads(item["text"])
                        if isinstance(parsed, list):
                            pages.extend(parsed)
                    except Exception:
                        pass
            if pages:
                return pages

        # Fallbacks for older / alternative mcp-atlassian response shapes
        if isinstance(result, str):
            try:
                result = _json.loads(result)
            except Exception:
                pass
        if isinstance(result, dict):
            return result.get("results", [])
        if isinstance(result, list):
            return result
        return []
    except Exception as exc:
        logger.warning("confluence_search_failed", query=query, error=str(exc))
        return []


async def _get_page_content(tools: list, page_id: str) -> tuple[str, Optional[str]]:
    """
    Fetch the full content of a Confluence page by ID.

    Returns:
        (content, error_message): content string and optional error message
    """
    get_tool = (
        find_tool(tools, "confluence_get_page")
        or find_tool(tools, "get_page", "confluence")
        or find_tool(tools, "page", "get")
    )
    if get_tool is None:
        return "", "tool_not_found"

    try:
        result = await get_tool.ainvoke({"page_id": page_id})
        if isinstance(result, dict):
            # mcp-atlassian returns body.storage.value for Confluence pages
            body = result.get("body", {})
            if isinstance(body, dict):
                storage = body.get("storage", {})
                if isinstance(storage, dict):
                    raw = storage.get("value", "")
                    return _strip_html(raw), None
            # Fallback: try direct content field
            raw = str(result.get("content", result.get("body", "")))
            return _strip_html(raw), None
        return _strip_html(str(result)), None
    except Exception as exc:
        error_str = str(exc).lower()
        # Detect permission-related errors
        if "permission" in error_str or "does not have permission" in error_str:
            logger.warning(
                "confluence_permission_denied",
                page_id=page_id,
                error=str(exc)
            )
            return "", "permission_denied"
        elif "not found" in error_str or "no content" in error_str:
            logger.warning("confluence_page_not_found", page_id=page_id, error=str(exc))
            return "", "not_found"
        else:
            logger.warning("confluence_get_page_failed", page_id=page_id, error=str(exc))
            return "", "unknown_error"



def _page_url(page: dict) -> str:
    """Extract the page URL from an mcp-atlassian search result."""
    links = page.get("_links", {})
    if links.get("webui"):
        base = settings.confluence_url.rstrip("/")
        return f"{base}{links['webui']}"
    return page.get("url", page.get("self", ""))


async def _gather_confluence_data(
    ticket_context,
    space_keys: list[str],
    max_pages: int,
) -> tuple[list[dict], list[str], int, dict]:
    """
    Search Confluence and retrieve page content.

    Returns:
        pages_with_content: list of dicts with page metadata + content
        queries_used: list of search query strings
        total_searched: total number of raw search hits examined
        error_stats: dict with error counts (permission_errors, not_found, etc.)
    """
    if not settings.confluence_url:
        return [], [], 0, {}

    queries = _build_confluence_queries(ticket_context)
    logger.info("confluence_queries_built", queries=queries)

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        cf_tools = filter_confluence_tools(all_tools)

        if not cf_tools:
            logger.warning("no_confluence_tools_available")
            return [], queries, 0, {}

        # Run all queries in parallel
        search_tasks = [
            asyncio.create_task(_search_confluence(cf_tools, space_keys, q))
            for q in queries
        ]
        # Track which pages appeared in results for each query (for relevance scoring)
        page_hit_count: dict[str, int] = {}
        page_objects: dict[str, dict] = {}
        for task in search_tasks:
            results = await task
            for page in results:
                pid = str(page.get("id", page.get("page_id", "")))
                if pid:
                    page_hit_count[pid] = page_hit_count.get(pid, 0) + 1
                    page_objects[pid] = page

        total_searched = len(page_objects)

        # Rank by hit count (pages matching more queries rank higher), then limit
        ranked_ids = sorted(page_objects, key=lambda pid: page_hit_count[pid], reverse=True)
        unique_pages = [page_objects[pid] for pid in ranked_ids[:max_pages]]

        # Fetch full content for each unique page
        content_tasks = [
            asyncio.create_task(
                _get_page_content(
                    cf_tools,
                    str(page.get("id", page.get("page_id", ""))),
                )
            )
            for page in unique_pages
        ]
        pages_with_content = []
        permission_errors = 0
        not_found_errors = 0
        other_errors = 0

        for page, content_task in zip(unique_pages, content_tasks):
            content, error = await content_task

            # Track error types
            if error == "permission_denied":
                permission_errors += 1
            elif error == "not_found":
                not_found_errors += 1
            elif error:
                other_errors += 1

            # Fall back to the snippet embedded in the search result if MCP fetch returns empty
            if not content:
                embedded = page.get("content", {})
                if isinstance(embedded, dict):
                    content = _strip_html(embedded.get("value", ""))
                elif isinstance(embedded, str):
                    content = _strip_html(embedded)
            if content:  # Only include page if we have some content
                pid = str(page.get("id", page.get("page_id", "")))
                pages_with_content.append({
                    **page,
                    "_fetched_content": content,
                    "_query_hits": page_hit_count.get(pid, 1),
                })

        # Log summary of errors if any occurred
        if permission_errors > 0:
            logger.warning(
                "confluence_permission_errors_summary",
                permission_errors=permission_errors,
                message=f"{permission_errors} Confluence page(s) could not be accessed due to permission errors. "
                        f"Check JIRA_API_TOKEN permissions or CONFLUENCE_SPACE_KEYS filter in .env"
            )
        if not_found_errors > 0:
            logger.info("confluence_not_found_errors", count=not_found_errors)
        if other_errors > 0:
            logger.warning("confluence_other_errors", count=other_errors)

        error_stats = {
            "permission_errors": permission_errors,
            "not_found": not_found_errors,
            "other_errors": other_errors,
        }

    return pages_with_content, queries, total_searched, error_stats


def _format_pages_for_prompt(pages_with_content: list[dict]) -> str:
    """Format pages into a readable block for the LLM prompt."""
    if not pages_with_content:
        return "(no Confluence pages retrieved)"

    parts = []
    for i, page in enumerate(pages_with_content, start=1):
        title = page.get("title", "Untitled")
        space = page.get("space", {})
        space_key = space.get("key", "") if isinstance(space, dict) else str(space)
        url = _page_url(page)
        content = page.get("_fetched_content", "")
        hits = page.get("_query_hits", 1)
        relevance_note = f"matched {hits} search quer{'y' if hits == 1 else 'ies'}"
        parts.append(
            f"### Page {i}: {title}\n"
            f"Space: {space_key} | {relevance_note} | URL: {url}\n\n"
            f"{content or '(content unavailable)'}"
        )
    return "\n\n---\n\n".join(parts)


# ── Agent class ───────────────────────────────────────────────────────────────


class ConfluenceAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.FETCHING_CONFLUENCE_DOCS,
        )

        if ticket_context is None:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["confluence_agent: ticket_context is None"],
                "should_stop": True,
            }

        # If Confluence is not configured, return an empty context and continue
        if not settings.confluence_url:
            self.logger.info(
                "confluence_skipped_not_configured",
                ticket_id=ticket_id,
                run_id=run_id,
            )
            return {
                "confluence_context": ConfluenceContext(
                    summary="Confluence not configured — no documentation context retrieved.",
                ),
                "current_phase": WorkflowPhase.PLANNING,
            }

        try:
            space_keys = settings.confluence_space_keys_list
            max_pages = settings.confluence_max_pages

            try:
                pages_with_content, queries_used, total_searched, error_stats = self.run_async(
                    _gather_confluence_data(ticket_context, space_keys, max_pages)
                )
            except (OSError, RuntimeError) as mcp_error:
                # MCP client can fail on Windows with OSError. Log and continue without Confluence.
                self.logger.warning(
                    "confluence_mcp_failed",
                    ticket_id=ticket_id,
                    error=str(mcp_error),
                )
                return {
                    "confluence_context": ConfluenceContext(
                        summary=f"Confluence MCP client failed: {str(mcp_error)[:100]}",
                    ),
                    "current_phase": WorkflowPhase.PLANNING,
                }

            pages_content_text = _format_pages_for_prompt(pages_with_content)

            human_prompt = CONFLUENCE_HUMAN_TEMPLATE.format(
                ticket_id=ticket_id,
                title=ticket_context.title,
                description=ticket_context.description or "(empty)",
                acceptance_criteria=getattr(ticket_context, "acceptance_criteria", None) or "(not provided)",
                space_keys=", ".join(space_keys) or "(all spaces)",
                total_pages=total_searched,
                pages_content=pages_content_text,
            )

            result, call_id = self.invoke_llm_structured(
                system_prompt=CONFLUENCE_SYSTEM,
                human_prompt=human_prompt,
                output_schema=ConfluenceContext,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="confluence_context_analysis",
            )

            if result is None:
                raise ValueError("LLM returned None for ConfluenceContext")

            # Backfill metadata the LLM cannot know
            result.total_pages_searched = total_searched
            result.search_queries_used = queries_used

            self.logger.info(
                "confluence_docs_retrieved",
                ticket_id=ticket_id,
                run_id=run_id,
                pages_found=len(result.pages_found),
                total_searched=total_searched,
                doc_update_suggestions=len(result.doc_update_suggestions),
            )

            # Warn user if there were permission errors
            warnings = []
            if error_stats.get("permission_errors", 0) > 0:
                warnings.append(
                    f"⚠️ Warning: {error_stats['permission_errors']} Confluence page(s) could not be accessed due to permission errors. "
                    f"This may result in incomplete documentation context. "
                    f"Check JIRA_API_TOKEN permissions or CONFLUENCE_SPACE_KEYS in .env"
                )
                # Print to console so user sees it
                print("\n" + warnings[0])

            return {
                "confluence_context": result,
                "current_phase": WorkflowPhase.PLANNING,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
                "mcp_tool_calls": [
                    {
                        "tool": "confluence_search",
                        "queries": queries_used,
                        "pages_retrieved": len(pages_with_content),
                    }
                ],
                "warnings": warnings,
            }

        except Exception as exc:
            self.logger.error(
                "agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id
            )
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"confluence_agent: {exc}"],
                "should_stop": True,
            }


_agent = ConfluenceAgent()


def confluence_agent_node(state: WorkflowState) -> dict:
    return _agent.run(state)
