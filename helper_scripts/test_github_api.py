#!/usr/bin/env python3
"""Test GitHub API to see what we can fetch"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from mcp_client.client_factory import get_mcp_client, filter_github_tools
from utils.mcp_helpers import find_tool

async def test_github():
    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    print(f"Testing GitHub API for {owner}/{repo}")
    print("=" * 70)

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        gh_tools = filter_github_tools(all_tools)

        print(f"\nGitHub tools available: {len(gh_tools)}")

        # Test 1: Get file contents with empty path
        print("\n1. Testing get_file_contents with path=''...")
        get_contents = find_tool(gh_tools, "get_file_contents")
        if get_contents:
            try:
                result = await get_contents.ainvoke({"owner": owner, "repo": repo, "path": ""})
                print(f"   Result type: {type(result)}")
                if isinstance(result, list):
                    print(f"   List length: {len(result)}")
                    if len(result) > 0:
                        print(f"   First item: {result[0]}")
                elif isinstance(result, dict):
                    print(f"   Dict keys: {result.keys()}")
                else:
                    print(f"   Result: {str(result)[:200]}")
            except Exception as e:
                print(f"   ERROR: {e}")

        # Test 2: Search repositories
        print("\n2. Testing search_repositories...")
        search_repos = find_tool(gh_tools, "search_repositories")
        if search_repos:
            try:
                result = await search_repos.ainvoke({"q": f"repo:{owner}/{repo}"})
                print(f"   Result type: {type(result)}")
                if isinstance(result, dict):
                    print(f"   Keys: {result.keys()}")
                    if "items" in result:
                        print(f"   Items: {len(result['items'])}")
            except Exception as e:
                print(f"   ERROR: {e}")

        # Test 3: List commits
        print("\n3. Testing list_commits...")
        list_commits = find_tool(gh_tools, "list_commits")
        if list_commits:
            try:
                result = await list_commits.ainvoke({"owner": owner, "repo": repo, "per_page": 5})
                print(f"   Result type: {type(result)}")
                if isinstance(result, list):
                    print(f"   Commits found: {len(result)}")
                    if len(result) > 0:
                        print(f"   First commit SHA: {result[0].get('sha', 'N/A')[:8]}")
            except Exception as e:
                print(f"   ERROR: {e}")

        # Test 4: Search code
        print("\n4. Testing search_code...")
        search_code = find_tool(gh_tools, "search_code")
        if search_code:
            try:
                result = await search_code.ainvoke({"q": f"repo:{owner}/{repo}"})
                print(f"   Result type: {type(result)}")
                if isinstance(result, dict):
                    items = result.get("items", [])
                    print(f"   Files found: {len(items)}")
                    if items:
                        print(f"   First file: {items[0].get('path', 'N/A')}")
            except Exception as e:
                print(f"   ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(test_github())
