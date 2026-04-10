# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langchain_mcp_adapters.client import MultiServerMCPClient

from config.settings import settings
from app_logging.activity_logger import ActivityLogger

logger = ActivityLogger("mcp_client_factory")


def _build_server_config() -> dict:
    """
    Build the server configuration dict for MultiServerMCPClient.

    Jira + Confluence: uvx mcp-atlassian  (stdio transport, API-token auth)
      mcp-atlassian serves both Jira and Confluence tools from the same process.
      Confluence vars are added when CONFLUENCE_URL is configured.
    GitHub: npx @modelcontextprotocol/server-github  (stdio transport, PAT auth)
    """
    return {
        "jira": {
            "command": "uvx",
            "args": ["mcp-atlassian"],
            "env": {
                "JIRA_URL": settings.jira_url,
                "JIRA_USERNAME": settings.jira_username,
                "JIRA_API_TOKEN": settings.jira_api_token.get_secret_value(),
                **(
                    {"JIRA_PROJECTS_FILTER": settings.jira_projects_filter}
                    if settings.jira_projects_filter
                    else {}
                ),
                # Confluence uses the same mcp-atlassian process and the same credentials
                **(
                    {
                        "CONFLUENCE_URL": settings.confluence_url,
                        "CONFLUENCE_USERNAME": settings.jira_username,
                        "CONFLUENCE_API_TOKEN": settings.jira_api_token.get_secret_value(),
                        **(
                            {"CONFLUENCE_SPACES_FILTER": settings.confluence_space_keys}
                            if settings.confluence_space_keys
                            else {}
                        ),
                    }
                    if settings.confluence_url
                    else {}
                ),
            },
            "transport": "stdio",
        },
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token,
            },
            "transport": "stdio",
        },
    }


@asynccontextmanager
async def get_pr_mcp_client() -> AsyncIterator[MultiServerMCPClient]:
    """
    Async context manager for GitHub MCP client using the PR-specific token.
    Used when creating draft PRs on the configured PR target repo.
    Falls back to the scout token if no PR token is configured.
    """
    pr_token = settings.github_pr_token.get_secret_value()
    token = pr_token if pr_token else settings.github_token

    config = {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": token},
            "transport": "stdio",
        }
    }
    logger.info("pr_mcp_client_initializing", owner=settings.github_pr_repo_owner)
    client = MultiServerMCPClient(config)
    available_tools = await client.get_tools()
    logger.info("pr_mcp_client_ready", tool_count=len(available_tools))
    yield client
    logger.info("pr_mcp_client_closed")


@asynccontextmanager
async def get_mcp_client() -> AsyncIterator[MultiServerMCPClient]:
    """
    Async context manager that starts both MCP server subprocesses,
    waits for them to be ready, and yields a connected MultiServerMCPClient.

    Usage:
        async with get_mcp_client() as client:
            tools = await client.get_tools()
            jira_tools = [t for t in tools if "jira" in t.name.lower()]
    """
    config = _build_server_config()
    logger.info("mcp_client_initializing", servers=list(config.keys()))

    client = MultiServerMCPClient(config)
    available_tools = await client.get_tools()
    tool_names = [t.name for t in available_tools]
    logger.info(
        "mcp_client_ready",
        tool_count=len(available_tools),
        tool_names=tool_names,
    )
    yield client
    logger.info("mcp_client_closed")


def filter_jira_tools(tools: list) -> list:
    """Return only Jira-related tools from the full tool list."""
    keywords = {"jira", "issue", "comment", "atlassian", "project", "transition"}
    return [t for t in tools if any(kw in t.name.lower() for kw in keywords)]


def filter_github_tools(tools: list) -> list:
    """Return only GitHub-related tools from the full tool list."""
    keywords = {
        "github",
        "repo",
        "pull_request",
        "create_pull",
        "get_file",
        "create_or_update",
        "search_code",
        "push",
        "branch",
        "commit",
        "content",
    }
    return [t for t in tools if any(kw in t.name.lower() for kw in keywords)]


def filter_confluence_tools(tools: list) -> list:
    """Return only Confluence-related tools from the full tool list."""
    keywords = {"confluence", "page", "space", "wiki"}
    return [t for t in tools if any(kw in t.name.lower() for kw in keywords)]
