# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/context_assembler.py
============================
Assembles a rich, structured context block from the code-intelligence
knowledge base for consumption by the planner and explorer agents.

This replaces the old RAG retriever approach: instead of a vector-similarity
lookup, we use the curated knowledge base (file summaries, repo map, scope
baselines, co-change graphs) to build grounded context for each ticket.

No LLM call is made here — this node is deterministic and fast.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app_logging.activity_logger import ActivityLogger
from code_intelligence.knowledge_base import (
    get_scope_baseline,
    get_summaries_for_context,
    load_repo_map,
    get_related_files,
    search_by_concepts,
)
from schemas.workflow_state import WorkflowState

logger = ActivityLogger("context_assembler")

# ---------------------------------------------------------------------------
# Domain keyword → module mapping  (Tier 3 hardcoded fallback)
# ---------------------------------------------------------------------------

_MODULE_KEYWORDS_HARDCODED: dict[str, list[str]] = {
    "banking": [
        "bank", "account", "nacha", "payment", "banking", "inherit", "payout",
    ],
    "organizations": [
        "org", "organization", "hierarchy", "sub-org", "main org", "combine",
        "reconcil", "dropdown", "flip", "detach", "reassign",
    ],
    "cases": [
        "case", "dispute", "eligibility", "upload", "ip", "nip",
    ],
    "payments": [
        "payment", "invoice", "payout", "hold", "stripe", "quickbooks",
        "csv", "export",
    ],
    "auth": [
        "permission", "role", "auth", "access", "view", "member",
    ],
    "email": [
        "email", "welcome", "template", "notification",
    ],
    "admin": [
        "admin", "dashboard", "report",
    ],
    "cms": [
        "cms", "case management", "invoice", "reconcil",
    ],
}

_MODULE_KEYWORDS_JSON = (
    Path(__file__).parent.parent / "code_intelligence" / "data" / "module_keywords.json"
)

# Stopwords excluded from raw token extraction
_CONCEPT_STOPWORDS = {
    "should", "will", "need", "must", "want", "have", "been", "with",
    "from", "that", "this", "when", "user", "users", "able", "into",
    "also", "some", "more", "than", "then", "they", "them", "their",
    "page", "form", "modal", "button", "table", "item", "items",
    "value", "object", "string", "number", "boolean", "array", "list",
    "error", "loading", "state", "props", "params", "result",
    "response", "request", "import", "export", "default",
    "null", "undefined", "none", "true", "false", "type", "data",
}


def _load_module_keywords() -> dict[str, list[str]]:
    """
    3-tier module keyword loading:
      Tier 1: module_keywords.json  (learned from KB via build_module_keywords.py)
      Tier 3: hardcoded fallback    (always available)
    Returns merged dict: Tier-1 entries merged over Tier-3, so hardcoded
    modules not in the JSON are still available.
    """
    merged = dict(_MODULE_KEYWORDS_HARDCODED)
    if _MODULE_KEYWORDS_JSON.exists():
        try:
            with open(_MODULE_KEYWORDS_JSON, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                # Tier-1 keywords take precedence; merge new modules in
                for mod, kws in loaded.items():
                    if isinstance(kws, list) and kws:
                        merged[mod] = kws
        except Exception as exc:
            logger.warning("module_keywords_load_failed", error=str(exc))
    return merged

# How many characters of co-change hint text to include per file
_CO_CHANGE_SNIPPET_LIMIT = 400

# Maximum number of related files to include in co-change hints
_MAX_CO_CHANGE_FILES = 5


def _truncate_at_boundary(text: str, max_chars: int, sep: str = "\n\n") -> str:
    """Truncate text at the last complete block boundary before max_chars.

    Cuts at the last occurrence of sep so no entry is split mid-way, preventing
    the planner from seeing a half-written file path or partial summary that could
    cause hallucination. Falls back to the last newline, then hard-cuts only as
    a last resort.
    """
    if len(text) <= max_chars:
        return text
    idx = text.rfind(sep, 0, max_chars)
    if idx > max_chars // 2:
        return text[:idx].rstrip()
    # Fall back to single newline boundary
    idx = text.rfind("\n", 0, max_chars)
    if idx > max_chars // 2:
        return text[:idx].rstrip()
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _extract_concepts(text: str) -> list[str]:
    """
    Extract meaningful domain concept tokens from free-form ticket text.

    Uses a broader extraction than before — collects all 4+ char non-stopword
    tokens, not just those already in the hardcoded keyword list.  This gives
    search_by_concepts() richer input so it can find more KB matches.
    De-duplicates while preserving insertion order.
    """
    raw_tokens = re.split(r"[^a-z0-9\-]+", text.lower())
    seen: set[str] = set()
    concepts: list[str] = []

    for token in raw_tokens:
        token = token.strip("-")
        if len(token) < 3 or token in _CONCEPT_STOPWORDS or token in seen:
            continue
        concepts.append(token)
        seen.add(token)

    return concepts


def _detect_primary_module(concepts: list[str], module_keywords: dict[str, list[str]]) -> str:
    """
    3-tier primary module detection:

    Tier 1 (KB-learned): Count how many KB files matching *concepts* belong
      to each module.  Requires ≥2 hits to trust.
    Tier 2 (Keywords): Score modules by keyword overlap with concepts using
      the loaded module_keywords (Tier-1 JSON + Tier-3 hardcoded).
    Tier 3 (Fallback): Return "general".
    """
    # ── Tier 1: KB file-module scoring ────────────────────────────────────
    if concepts:
        try:
            matching = search_by_concepts(concepts, top_k=30)
            kb_scores: dict[str, int] = {}
            for f in matching:
                mod = (f.get("module") or "general").lower()
                kb_scores[mod] = kb_scores.get(mod, 0) + 1
            if kb_scores:
                best_mod, best_count = max(kb_scores.items(), key=lambda x: x[1])
                if best_count >= 2:
                    return best_mod
        except Exception:
            pass

    # ── Tier 2: Keyword scoring from loaded module_keywords ────────────────
    concept_set = set(concepts)
    scores: dict[str, int] = {}
    for module, keywords in module_keywords.items():
        score = sum(1 for kw in keywords if kw in concept_set or
                    any(kw in c for c in concept_set))
        if score > 0:
            scores[module] = score

    if scores:
        return max(scores, key=lambda m: scores[m])

    # ── Tier 3: Fallback ──────────────────────────────────────────────────
    return "general"


def _build_co_change_hints(primary_module: str, concepts: list[str]) -> str:
    """
    Build a short text block describing co-change patterns for the files
    most relevant to *concepts* inside *primary_module*.

    Uses search_by_concepts to find relevant files, then get_related_files
    for each to discover co-change partners.
    """
    hints_parts: list[str] = []
    seen_files: set[str] = set()

    try:
        # Find top files matching the ticket's concepts
        relevant = search_by_concepts(concepts, top_k=5)
        for entry in relevant[:_MAX_CO_CHANGE_FILES]:
            file_path = entry.get("path", "")
            if not file_path or file_path in seen_files:
                continue
            seen_files.add(file_path)

            # Get co-change partners for this file
            related = get_related_files(file_path, top_k=4)
            if related:
                co_list = ", ".join(related[:4])
                hints_parts.append(
                    f"  {file_path} ↔ frequently changed with: {co_list}"
                )
            else:
                hints_parts.append(f"  {file_path} — (no co-change data)")
    except Exception as exc:
        logger.warning("co_change_hints_failed", error=str(exc))

    if not hints_parts:
        return "(no co-change data available)"

    return "\n".join(hints_parts)


def _extract_repo_map_text_section(repo_map_text: str, module: str) -> str:
    """
    Extract the section of a plain-text repo map relevant to *module*.

    The repo map groups lines by directory. We look for directory paths
    that contain the module name and return those sections.
    Falls back to the first 3000 chars if no module-specific section is found.
    """
    if not repo_map_text:
        return "(repo map unavailable)"

    module_lower = module.lower()

    # Module name → likely directory keywords
    _MODULE_DIR_HINTS: dict[str, list[str]] = {
        "banking": ["banking", "bank", "nacha", "payment"],
        "organizations": ["organization", "org-", "hierarchy"],
        "cases": ["case", "dispute", "eligibility"],
        "payments": ["payment", "invoice", "payout", "stripe"],
        "auth": ["auth", "permission", "role", "session"],
        "email": ["email", "mail", "template"],
        "admin": ["admin"],
    }
    dir_hints = _MODULE_DIR_HINTS.get(module_lower, [module_lower])

    # Collect lines that match the module's directory hints
    matched_lines: list[str] = []
    for line in repo_map_text.splitlines():
        line_lower = line.lower()
        if any(hint in line_lower for hint in dir_hints):
            matched_lines.append(line)

    if matched_lines:
        result = "\n".join(matched_lines)
        return _truncate_at_boundary(result, 3000, sep="\n")

    # Fallback: return first 3000 chars as general overview
    return _truncate_at_boundary(repo_map_text, 3000, sep="\n")


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


def context_assembler_node(state: WorkflowState) -> dict:
    """
    LangGraph node — assembles the rich context dictionary for downstream
    planner / explorer agents.

    Reads:
        state["ticket_context"]  — TicketContext
        state["ticket_type"]     — str (from ticket_classifier_node)

    Writes:
        state["assembled_context"] — dict with keys:
            repo_map_section       : str   (~3K tokens of module overview)
            file_summaries_section : str   (~5K tokens of relevant summaries)
            scope_baseline         : dict  (expected file-count ranges)
            co_change_hints        : str   (co-change patterns for top files)
            module_name            : str   (detected primary domain module)
    """
    ticket_context = state.get("ticket_context")
    ticket_type = state.get("ticket_type") or "other"
    ticket_id = state.get("ticket_id", "unknown")
    run_id = state.get("run_id", "unknown")

    logger.info(
        "agent_node_entered",
        ticket_id=ticket_id,
        run_id=run_id,
        ticket_type=ticket_type,
        phase="context_assembly",
    )

    # ── 1. Build combined ticket text ──────────────────────────────────────
    if ticket_context is not None:
        title = getattr(ticket_context, "title", "") or ""
        description = getattr(ticket_context, "description", "") or ""
        ac = getattr(ticket_context, "acceptance_criteria", "") or ""
        combined_text = f"{title} {description} {ac}"
    else:
        combined_text = ""
        logger.warning(
            "context_assembler_no_ticket",
            ticket_id=ticket_id,
            run_id=run_id,
        )

    # ── 2. Extract concepts and detect primary module (3-tier) ────────────
    module_keywords = _load_module_keywords()
    concepts = _extract_concepts(combined_text)
    primary_module = _detect_primary_module(concepts, module_keywords)

    logger.info(
        "context_assembler_concepts",
        ticket_id=ticket_id,
        concepts=concepts[:10],
        primary_module=primary_module,
    )

    # ── 3. Fetch file summaries from knowledge base ────────────────────────
    file_summaries_section = "(no file summaries available)"
    try:
        # get_summaries_for_context returns a ready-to-inject string
        summaries_text = get_summaries_for_context(concepts, module=primary_module)
        if summaries_text:
            file_summaries_section = _truncate_at_boundary(summaries_text, 5000)
    except Exception as exc:
        logger.warning("file_summaries_failed", ticket_id=ticket_id, error=str(exc))

    # ── 4. Load repo map and extract relevant section ──────────────────────
    repo_map_section = "(repo map unavailable)"
    try:
        repo_map_text = load_repo_map()
        if repo_map_text:
            # repo_map is a plain text string — extract lines relevant to primary_module
            repo_map_section = _extract_repo_map_text_section(repo_map_text, primary_module)
    except Exception as exc:
        logger.warning("repo_map_failed", ticket_id=ticket_id, error=str(exc))

    # ── 5. Get scope baseline for ticket type ─────────────────────────────
    scope_baseline: dict = {}
    try:
        scope_baseline = get_scope_baseline(ticket_type) or {}
    except Exception as exc:
        logger.warning("scope_baseline_failed", ticket_id=ticket_id, error=str(exc))
        scope_baseline = {
            "avg_files": 3,
            "min_files": 1,
            "max_files": 8,
            "ticket_type": ticket_type,
        }

    # ── 6. Build co-change hints ───────────────────────────────────────────
    co_change_hints = _build_co_change_hints(primary_module, concepts)

    assembled_context = {
        "repo_map_section": repo_map_section,
        "file_summaries_section": file_summaries_section,
        "scope_baseline": scope_baseline,
        "co_change_hints": co_change_hints,
        "module_name": primary_module,
    }

    logger.info(
        "agent_node_completed",
        ticket_id=ticket_id,
        run_id=run_id,
        module=primary_module,
        concepts_found=len(concepts),
        summaries_chars=len(file_summaries_section),
        repo_map_chars=len(repo_map_section),
    )

    return {"assembled_context": assembled_context}
