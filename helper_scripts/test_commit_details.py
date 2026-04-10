#!/usr/bin/env python3
"""Test if we can get commit file details from GitHub MCP"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from mcp_client.client_factory import get_mcp_client, filter_github_tools
from utils.mcp_helpers import find_tool

async def test():
    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    print(f"Testing commit details for {owner}/{repo}")
    print("=" * 70)

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        gh_tools = filter_github_tools(all_tools)

        # First get some commits
        print("\n1. Fetching recent commits...")
        list_commits = find_tool(gh_tools, "list_commits")
        if not list_commits:
            print("ERROR: list_commits tool not found")
            return

        result = await list_commits.ainvoke({"owner": owner, "repo": repo, "per_page": 5})

        commits = []
        if isinstance(result, list) and len(result) > 0:
            first_item = result[0]
            if isinstance(first_item, dict) and "text" in first_item:
                commits = json.loads(first_item["text"])
            elif isinstance(first_item, dict):
                commits = result

        print(f"   Found {len(commits)} commits")

        if not commits:
            print("   ERROR: No commits returned")
            return

        # Get first commit SHA
        first_commit = commits[0]
        sha = first_commit.get("sha") or first_commit.get("id")
        print(f"   First commit SHA: {sha[:8] if sha else 'N/A'}")

        # Now try to get commit details
        print("\n2. Testing get_commit tool...")
        get_commit = find_tool(gh_tools, "get_commit") or find_tool(gh_tools, "commit")

        if not get_commit:
            print("   ERROR: get_commit tool not found!")
            print(f"   Available GitHub tools: {[t.name for t in gh_tools]}")
            return

        print(f"   Fetching details for commit {sha[:8]}...")
        result = await get_commit.ainvoke({"owner": owner, "repo": repo, "ref": sha})

        print(f"   Result type: {type(result)}")

        commit_data = None
        if isinstance(result, list) and len(result) > 0:
            first_item = result[0]
            if isinstance(first_item, dict) and "text" in first_item:
                try:
                    commit_data = json.loads(first_item["text"])
                except:
                    commit_data = first_item
            elif isinstance(first_item, dict):
                commit_data = first_item
            else:
                commit_data = result  # It's a list of something else
        elif isinstance(result, dict):
            commit_data = result

        print(f"   Commit data type: {type(commit_data)}")

        if isinstance(commit_data, list):
            print(f"   Commit data is a list with {len(commit_data)} items")
            if len(commit_data) > 0:
                print(f"   First item type: {type(commit_data[0])}")
                print(f"   First item: {str(commit_data[0])[:200]}")
            return

        if commit_data:
            files = commit_data.get("files", [])
            print(f"   Files in commit: {len(files)}")

            if files:
                print("\n   First 5 files:")
                for f in files[:5]:
                    filename = f.get("filename", "?")
                    status = f.get("status", "?")
                    print(f"      {status}: {filename}")
            else:
                print("   WARNING: No files array in commit data")
                print(f"   Keys in commit data: {list(commit_data.keys())[:10]}")
        else:
            print("   ERROR: Could not parse commit data")
            print(f"   Raw result: {str(result)[:500]}")

if __name__ == "__main__":
    asyncio.run(test())
