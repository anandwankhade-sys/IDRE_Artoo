# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Optional
from agents.base_agent import BaseAgent
from config.settings import settings
from app_logging.activity_logger import ActivityLogger
from mcp_client.client_factory import filter_github_tools, get_mcp_client
from prompts.repo_scout_prompt import REPO_SCOUT_HUMAN_TEMPLATE, REPO_SCOUT_SYSTEM
from schemas.repo import FileAnalysis, RepoContext
from schemas.workflow_state import WorkflowPhase, WorkflowState
from utils.mcp_helpers import find_tool
from utils.retry import ainvoke_with_retry
from utils.text_helpers import extract_keywords as _extract_keywords

logger = ActivityLogger("repo_scout_agent")


def _get_local_git_history(path: str) -> str:
    """Get recent commit history for a specific file from the local repository.

    NOTE: This is optional and only used if a local git repo is available.
    The pipeline works fine without it, using GitHub MCP instead.
    """
    repo_dir = os.path.join(os.getcwd(), "idre")
    if not os.path.exists(repo_dir):
        return ""  # Silently skip if no local repo

    try:
        # Run git log -n 5 --oneline -- <path>
        # Use -C repo_dir to ensure we run context in the repo
        cmd = ["git", "-C", repo_dir, "log", "-n", "5", "--oneline", "--", path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        return ""
    except Exception:
        return ""  # Silently skip on any error


async def _get_repo_tree(tools: list, owner: str, repo: str) -> str:
    """Fetch top-level directory structure."""
    get_contents = find_tool(tools, "get_file_contents") or find_tool(tools, "contents")
    if get_contents is None:
        return "(directory listing unavailable)"

    try:
        result = await ainvoke_with_retry(get_contents, {"owner": owner, "repo": repo, "path": ""})
        if isinstance(result, list):
            entries = [
                f"{'📁' if item.get('type') == 'dir' else '📄'} {item.get('name', '')}"
                for item in result
            ]
            return "\n".join(entries)
        return str(result)
    except Exception as exc:
        return f"(error fetching tree: {exc})"


def _extract_mcp_text(result) -> str:
    """
    Normalise MCP tool output to a plain string.

    The GitHub MCP adapter may return results in different formats depending
    on the LLM provider:
      • dict  — standard LangChain MCP result (direct GitHub API JSON)
      • list  — Gemini content-block format: [{'type': 'text', 'text': '...'}]
      • str   — already a string

    Returns the extracted text, or empty string on failure.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result)
    if isinstance(result, list):
        # Gemini content-block format
        parts = []
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(result)


async def _get_file_content(tools: list, owner: str, repo: str, path: str) -> str:
    """
    Fetch the decoded source content of a specific file.

    Handles both direct dict results (Bedrock/OpenAI MCP) and Gemini content-block
    list results.  In both cases the GitHub API response JSON is parsed and the
    base64-encoded file content is decoded before returning.
    """
    import base64 as _b64
    import json as _json

    get_contents = find_tool(tools, "get_file_contents") or find_tool(tools, "contents")
    if get_contents is None:
        return ""
    try:
        raw = await ainvoke_with_retry(get_contents, {"owner": owner, "repo": repo, "path": path})

        # Step 1: normalise to a plain string
        text = _extract_mcp_text(raw)

        # Step 2: try to parse as GitHub API JSON and decode base64 content
        if text.strip().startswith("{"):
            try:
                data = _json.loads(text)
                content = data.get("content", "")
                if data.get("encoding") == "base64" and content:
                    # GitHub base64 includes newlines — strip them
                    decoded = _b64.b64decode(content.replace("\n", "")).decode("utf-8", errors="replace")
                    return decoded[:2000]  # 2K chars keeps prompt size manageable
                if content:
                    return str(content)[:2000]
            except Exception:
                pass

        # Step 3: fallback — return raw text (may already be file content)
        return text[:2000]

    except Exception as exc:
        logger.warning("file_fetch_failed", path=path, error=str(exc))
        return ""


async def _search_code(tools: list, owner: str, repo: str, query: str) -> list[str]:
    """Search for relevant files using keyword search."""
    search_tool = find_tool(tools, "search_code") or find_tool(tools, "search")
    if search_tool is None:
        return []
    try:
        result = await ainvoke_with_retry(search_tool, {"q": f"{query} repo:{owner}/{repo}"})
        items = result.get("items", []) if isinstance(result, dict) else []
        return [item.get("path", "") for item in items[:10]]
    except Exception as exc:
        logger.warning("code_search_failed", query=query, error=str(exc))
        return []


async def _get_kb_relevant_files(
    tools: list,
    owner: str,
    repo: str,
    ticket_context,
    max_files: int,
) -> tuple[str, list[str]]:
    """
    Use the knowledge base to find relevant files, then fetch their actual
    content from GitHub.

    This replaces the old approach of trying to fetch generic dependency files
    (requirements.txt, go.mod, etc.) which don't exist in a Next.js/TS repo.

    Returns (file_listing_text, list_of_paths).
    """
    from code_intelligence.knowledge_base import search_by_concepts

    # Extract keywords from ticket — use all meaningful tokens, not just 5
    title = getattr(ticket_context, "title", "") or ""
    description = getattr(ticket_context, "description", "") or ""
    ac = getattr(ticket_context, "acceptance_criteria", "") or ""
    full_text = f"{title} {description} {ac}"

    import re as _re
    _stop = {
        "should", "will", "need", "must", "want", "have", "been", "with",
        "from", "that", "this", "when", "user", "users", "able", "into",
        "also", "some", "more", "than", "then", "they", "them", "their",
    }
    raw_tokens = _re.findall(r"\b[a-z][a-z0-9_]{2,}\b", full_text.lower())
    concepts = list(dict.fromkeys(t for t in raw_tokens if t not in _stop))

    # Tier 1: KB concept search — finds files whose domain_concepts overlap
    # Cap at 8 files so we don't blow up the prompt with too much code content.
    # Each file gets up to 2000 chars, so 8 files = ~16K chars + overhead ≈ 5-6K tokens.
    _KB_FILE_CAP = 8
    kb_files: list[str] = []
    if concepts:
        try:
            matches = search_by_concepts(concepts, top_k=_KB_FILE_CAP + 4)
            kb_files = [m["path"] for m in matches if m.get("path")]
        except Exception as exc:
            logger.warning("kb_search_failed", error=str(exc))

    # Tier 2: GitHub code search as supplement if KB gives too few results
    search_supplement: list[str] = []
    if len(kb_files) < 3:
        search_kws = _extract_keywords(ticket_context)
        for kw in search_kws[:2]:
            try:
                found = await _search_code(tools, owner, repo, kw)
                search_supplement.extend(found)
            except Exception:
                pass

    # Merge, de-duplicate, limit to _KB_FILE_CAP
    all_paths = list(dict.fromkeys(kb_files + search_supplement))[:_KB_FILE_CAP]

    # Fetch actual file content from GitHub
    file_listing_parts: list[str] = []
    repo_dir = os.path.join(os.getcwd(), "idre")
    has_local = os.path.exists(repo_dir)

    for path in all_paths:
        content = await _get_file_content(tools, owner, repo, path)
        if content:
            history_text = ""
            if has_local:
                history = _get_local_git_history(path)
                if history:
                    history_text = f"\n#### Recent History (Local Git):\n```\n{history}\n```\n"
            file_listing_parts.append(f"### {path}\n```\n{content}\n```\n{history_text}")

    file_listing = "\n\n".join(file_listing_parts) or "(no matching files found in KB or GitHub)"
    logger.info(
        "repo_scout_kb_files_selected",
        kb_count=len(kb_files),
        supplement_count=len(search_supplement),
        fetched_count=len(file_listing_parts),
    )
    return file_listing, all_paths


async def _gather_repo_data(ticket_context, max_files: int) -> tuple[str, str, str, list[str]]:
    """Gather directory tree, KB-selected file contents, and relevant paths."""
    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        gh_tools = filter_github_tools(all_tools)

        # Parallel: repo tree + KB-driven file selection
        tree_task = asyncio.create_task(_get_repo_tree(gh_tools, owner, repo))
        kb_task = asyncio.create_task(
            _get_kb_relevant_files(gh_tools, owner, repo, ticket_context, max_files)
        )

        dir_summary = await tree_task
        file_listing, relevant_paths = await kb_task

        if os.path.exists(os.path.join(os.getcwd(), "idre")):
            logger.info("local_repo_detected", repo_path=os.path.join(os.getcwd(), "idre"))

        # dep_content kept for prompt compatibility — now contains KB summary note
        dep_content = (
            f"Note: This is a Next.js/TypeScript repository.\n"
            f"KB-selected {len(relevant_paths)} relevant files for this ticket.\n"
            f"See 'File Listing' section for actual code context."
        )

        return dir_summary, dep_content, file_listing, relevant_paths



class RepoScoutAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        run_id = state["run_id"]
        ticket_id = state["ticket_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase=WorkflowPhase.SCOUTING_REPO,
        )

        if ticket_context is None:
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": ["repo_scout: ticket_context is None"],
                "should_stop": True,
            }

        try:
            max_files = settings.repo_scout_max_files
            dir_summary, _, file_listing, relevant_paths = self.run_async(
                _gather_repo_data(ticket_context, max_files)
            )

            human_prompt = REPO_SCOUT_HUMAN_TEMPLATE.format(
                ticket_id=ticket_id,
                title=ticket_context.title,
                description=ticket_context.description or "(empty)",
                acceptance_criteria=getattr(ticket_context, "acceptance_criteria", None) or "(not provided)",
                repo_owner=settings.github_repo_owner,
                repo_name=settings.github_repo_name,
                directory_summary=dir_summary,
                file_listing=file_listing,
                max_files=max_files,
            )

            result, call_id = self.invoke_llm_structured(
                system_prompt=REPO_SCOUT_SYSTEM,
                human_prompt=human_prompt,
                output_schema=RepoContext,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="repo_scout_analysis",
            )

            if result is None:
                # Repo is likely empty or unrecognised — build a minimal context
                # so the pipeline can still produce a plan and code proposal.
                result = RepoContext(
                    repo_owner=settings.github_repo_owner,
                    repo_name=settings.github_repo_name,
                    directory_summary=dir_summary or "(repository appears empty)",
                    primary_language=None,
                )

            # Fill in metadata that LLM may not know
            result.repo_owner = settings.github_repo_owner
            result.repo_name = settings.github_repo_name
            result.directory_summary = dir_summary

            # If LLM returned 0 relevant_files but KB found concrete paths,
            # synthesise FileAnalysis entries so downstream agents have grounding.
            if not result.relevant_files and relevant_paths:
                from schemas.repo import FileAnalysis
                from code_intelligence.knowledge_base import get_file_summary
                synthesised: list[FileAnalysis] = []
                for path in relevant_paths[:10]:
                    summary = get_file_summary(path)
                    reason = summary.get("purpose", "Found via knowledge base concept search.") if summary else "Found via knowledge base concept search."
                    synthesised.append(FileAnalysis(
                        file_path=path,
                        language="TypeScript",
                        relevance_score=0.75,
                        relevance_reason=reason[:200],
                        summary=reason[:200],
                    ))
                result.relevant_files = synthesised
                self.logger.info(
                    "repo_scout_files_synthesised",
                    ticket_id=ticket_id,
                    count=len(synthesised),
                )

            self.logger.info(
                "github_repo_analyzed",
                ticket_id=ticket_id,
                run_id=run_id,
                relevant_files=len(result.relevant_files),
                impacted_modules=result.impacted_modules,
            )

            return {
                "repo_context": result,
                "current_phase": WorkflowPhase.PLANNING,
                "llm_call_ids": [call_id],
                "total_llm_calls": state.get("total_llm_calls", 0) + 1,
                "mcp_tool_calls": [
                    {"tool": "github_get_contents", "paths": relevant_paths}
                ],
            }

        except Exception as exc:
            self.logger.error("agent_node_failed", exc=exc, ticket_id=ticket_id, run_id=run_id)
            return {
                "current_phase": WorkflowPhase.FAILED,
                "errors": [f"repo_scout: {exc}"],
                "should_stop": True,
            }


_agent = RepoScoutAgent()


def repo_scout_node(state: WorkflowState) -> dict:
    return _agent.run(state)
