#!/usr/bin/env python3
# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
build_repo_map.py
=================
Builds repo_map.txt from GitHub repository using GitHub API.

Output format:
```
path/to/file.ts
  functionName(params)
  ClassName
  anotherFunction()
```

This file is used by:
- Context assembler (loads file structure)
- Code proposal agent (prevents hallucinations)
- File validator (checks if proposed files exist)
"""

import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Add parent directory to path so we can import from final/
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from mcp_client.client_factory import get_mcp_client, filter_github_tools
from utils.mcp_helpers import find_tool
from utils.retry import ainvoke_with_retry

# Extensions to include
SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".sql", ".prisma"}

# Output path
OUTPUT_FILE = Path(__file__).parent.parent / "code_intelligence" / "data" / "repo_map.txt"


async def get_repo_tree(tools: list) -> list[dict[str, Any]]:
    """
    Fetch complete directory tree from GitHub recursively.
    Returns list of file entries: {"path": "...", "type": "file"}
    """
    get_contents = find_tool(tools, "get_file_contents") or find_tool(tools, "contents")
    if get_contents is None:
        raise RuntimeError("GitHub get_file_contents tool not found")

    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    print(f"Fetching repository tree from {owner}/{repo}...")

    all_files: list[dict[str, Any]] = []

    async def fetch_dir(path: str = ""):
        """Recursively fetch directory contents"""
        try:
            result = await ainvoke_with_retry(
                get_contents,
                {"owner": owner, "repo": repo, "path": path}
            )

            # GitHub MCP returns: [{"type": "text", "text": "[JSON...]"}]
            # Need to parse the JSON from the text field
            items = []
            if isinstance(result, list) and len(result) > 0:
                first_item = result[0]
                if isinstance(first_item, dict) and "text" in first_item:
                    # Parse JSON from text field
                    text_content = first_item["text"]
                    items = json.loads(text_content)
                elif isinstance(first_item, dict):
                    # Direct list of items
                    items = result

            if isinstance(items, list):
                for item in items:
                    item_type = item.get("type")
                    item_path = item.get("path", "")
                    item_name = item.get("name", "")

                    # Skip hidden files, node_modules, build artifacts
                    if (item_name.startswith(".") or
                        item_name in ("node_modules", "__pycache__", ".next", "dist", "build")):
                        continue

                    if item_type == "file":
                        suffix = Path(item_path).suffix.lower()
                        if suffix in SOURCE_EXTS:
                            all_files.append(item)
                            if len(all_files) % 100 == 0:
                                print(f"  Found {len(all_files)} files...")

                    elif item_type == "dir":
                        # Recursively fetch subdirectory
                        await fetch_dir(item_path)

        except Exception as exc:
            print(f"Warning: Could not fetch {path}: {exc}")

    await fetch_dir("")
    print(f"Total files found: {len(all_files)}")
    return all_files


async def extract_symbols_from_content(content: str, file_path: str) -> list[str]:
    """
    Extract function/class names from file content.

    Returns list of symbol names (functions, classes, exports).
    """
    symbols: list[str] = []
    suffix = Path(file_path).suffix.lower()

    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        # TypeScript/JavaScript patterns

        # Functions: export function name(...) or function name(...)
        func_pattern = r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("
        symbols.extend(re.findall(func_pattern, content))

        # Classes: export class Name or class Name
        class_pattern = r"(?:export\s+)?class\s+(\w+)(?:\s+extends|\s*\{)"
        symbols.extend(re.findall(class_pattern, content))

        # Arrow functions: export const name = (...) =>
        arrow_pattern = r"(?:export\s+)?const\s+(\w+)\s*=\s*(?:\([^)]*\)|[^=]+)\s*=>"
        symbols.extend(re.findall(arrow_pattern, content))

        # Interfaces and types
        type_pattern = r"(?:export\s+)?(?:interface|type)\s+(\w+)"
        symbols.extend(re.findall(type_pattern, content))

    elif suffix == ".py":
        # Python patterns

        # Functions: def name(...)
        func_pattern = r"^(?:async\s+)?def\s+(\w+)\s*\("
        symbols.extend(re.findall(func_pattern, content, re.MULTILINE))

        # Classes: class Name
        class_pattern = r"^class\s+(\w+)(?:\(|\:)"
        symbols.extend(re.findall(class_pattern, content, re.MULTILINE))

    elif suffix == ".prisma":
        # Prisma models: model Name {
        model_pattern = r"^model\s+(\w+)\s*\{"
        symbols.extend(re.findall(model_pattern, content, re.MULTILINE))

        # Enums: enum Name {
        enum_pattern = r"^enum\s+(\w+)\s*\{"
        symbols.extend(re.findall(enum_pattern, content, re.MULTILINE))

    # Remove duplicates while preserving order
    seen = set()
    unique_symbols = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique_symbols.append(s)

    return unique_symbols


async def fetch_and_parse_file(tools: list, file_entry: dict) -> tuple[str, list[str]]:
    """
    Fetch file content from GitHub and extract symbols.

    Returns: (file_path, [symbol_names])
    """
    file_path = file_entry.get("path", "")

    get_contents = find_tool(tools, "get_file_contents") or find_tool(tools, "contents")
    if get_contents is None:
        return file_path, []

    try:
        owner = settings.github_repo_owner
        repo = settings.github_repo_name

        result = await ainvoke_with_retry(
            get_contents,
            {"owner": owner, "repo": repo, "path": file_path}
        )

        content = None

        # Handle MCP response format: [{"type": "text", "text": "{json}"}]
        if isinstance(result, list) and len(result) > 0:
            first_item = result[0]
            if isinstance(first_item, dict):
                if "text" in first_item:
                    # Parse JSON from text field
                    text_content = first_item["text"]
                    parsed = json.loads(text_content)
                    if isinstance(parsed, dict):
                        content = parsed.get("content", "")
                        encoding = parsed.get("encoding", "")
                    else:
                        content = text_content
                elif "content" in first_item:
                    content = first_item["content"]
                    encoding = first_item.get("encoding", "")
        elif isinstance(result, dict):
            content = result.get("content", "")
            encoding = result.get("encoding", "")

        if content:
            if encoding == "base64":
                try:
                    content = base64.b64decode(content).decode("utf-8", errors="replace")
                except Exception:
                    return file_path, []

            # Limit content size to avoid memory issues
            if len(content) > 100000:  # 100KB limit
                content = content[:100000]

            symbols = await extract_symbols_from_content(content, file_path)
            return file_path, symbols

    except Exception as exc:
        # Silently skip files that fail (binary files, permission issues, etc.)
        pass

    return file_path, []


async def build_repo_map():
    """Main function to build repo_map.txt"""

    print("=" * 70)
    print("Building repo_map.txt from GitHub")
    print("=" * 70)

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        gh_tools = filter_github_tools(all_tools)

        if not gh_tools:
            print("ERROR: No GitHub tools available. Check MCP client configuration.")
            return False

        # Step 1: Get complete file tree
        file_entries = await get_repo_tree(gh_tools)

        if not file_entries:
            print("ERROR: No files found in repository")
            return False

        # Step 2: Fetch and parse files in batches
        print("\nExtracting symbols from files...")

        results: list[tuple[str, list[str]]] = []

        # Process in batches to avoid overwhelming the API
        batch_size = 10
        for i in range(0, len(file_entries), batch_size):
            batch = file_entries[i:i+batch_size]
            batch_tasks = [fetch_and_parse_file(gh_tools, entry) for entry in batch]
            batch_results = await asyncio.gather(*batch_tasks)
            results.extend(batch_results)

            print(f"  Processed {min(i+batch_size, len(file_entries))}/{len(file_entries)} files...")

        # Step 3: Write to repo_map.txt
        print("\nWriting repo_map.txt...")
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for file_path, symbols in sorted(results):
                f.write(f"{file_path}\n")
                for symbol in symbols:
                    f.write(f"  {symbol}\n")

        file_size = OUTPUT_FILE.stat().st_size
        files_with_symbols = sum(1 for _, symbols in results if symbols)
        total_symbols = sum(len(symbols) for _, symbols in results)

        print("\n" + "=" * 70)
        print("✅ SUCCESS")
        print("=" * 70)
        print(f"Output file: {OUTPUT_FILE}")
        print(f"File size: {file_size:,} bytes ({file_size / 1024:.1f} KB)")
        print(f"Total files: {len(results)}")
        print(f"Files with symbols: {files_with_symbols}")
        print(f"Total symbols extracted: {total_symbols}")
        print("=" * 70)

        return True


if __name__ == "__main__":
    success = asyncio.run(build_repo_map())
    sys.exit(0 if success else 1)
