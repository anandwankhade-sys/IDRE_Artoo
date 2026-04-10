# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""Shared text-processing utilities."""
from __future__ import annotations

import re

_STOPWORDS = {
    "should", "will", "need", "must", "want", "have", "been", "with",
    "from", "that", "this", "when", "user", "users", "able", "into",
    "also", "some", "more", "than", "then", "they", "them", "their",
}


def extract_keywords(ticket_context, max_keywords: int = 5) -> list[str]:
    """Extract search keywords from ticket title and description.

    Filters common stop-words, deduplicates, and returns up to *max_keywords*
    lowercase tokens.
    """
    text = f"{ticket_context.title} {ticket_context.description or ''}"
    words = re.findall(r"\b[a-zA-Z][a-zA-Z_]{3,}\b", text)
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        lower = w.lower()
        if lower not in _STOPWORDS and lower not in seen:
            seen.add(lower)
            result.append(lower)
        if len(result) >= max_keywords:
            break
    return result
