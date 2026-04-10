# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""Shared MCP tool helpers."""
from __future__ import annotations

import json
from typing import Any, Optional


def find_tool(tools: list, *name_parts: str) -> Optional[Any]:
    """Return the first tool whose name contains ALL *name_parts* (case-insensitive).

    Examples::

        find_tool(tools, "search")
        find_tool(tools, "get_page", "confluence")
        find_tool(tools, "create_branch") or find_tool(tools, "branch", "create")
    """
    for tool in tools:
        name = tool.name.lower()
        if all(part in name for part in name_parts):
            return tool
    return None


def unwrap_tool_result(result: Any) -> dict:
    """Normalise the many shapes an MCP tool ``ainvoke()`` result can take.

    Tools with ``response_format='content_and_artifact'`` return a list of
    content blocks: ``[{"type": "text", "text": "<json string>"}, ...]``.
    This helper collapses them into a single plain ``dict``.
    """
    # Unpack (content, artifact) tuple if present
    if isinstance(result, tuple):
        result = result[0]

    if isinstance(result, dict):
        return result

    if isinstance(result, list):
        text_parts = []
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif hasattr(block, "text"):
                text_parts.append(block.text)
        text = "\n".join(text_parts)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"description": text}

    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"description": result}

    return result
