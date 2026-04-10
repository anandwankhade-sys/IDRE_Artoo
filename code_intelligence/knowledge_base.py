# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
"""
knowledge_base.py — Runtime loader and query interface for the IDRE code
intelligence knowledge base.  Used by agents; never writes files itself.

All public functions are safe to call even if the knowledge base has not been
built yet — they return empty / safe defaults in that case.
"""

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_DATA_DIR = _THIS_DIR / "data"
_SUMMARIES_FILE = _DATA_DIR / "file_summaries.json"
_REPO_MAP_FILE = _DATA_DIR / "repo_map.txt"
_CO_CHANGE_FILE = _DATA_DIR / "co_change.json"
_SCOPE_BASELINES_FILE = _DATA_DIR / "scope_baselines.json"

# ── Safe defaults ─────────────────────────────────────────────────────────────

_DEFAULT_BASELINES: dict[str, dict] = {
    "bug_fix":  {"avg_files": 3.0,  "p75_files": 5,  "count": 0},
    "feature":  {"avg_files": 7.0,  "p75_files": 12, "count": 0},
    "refactor": {"avg_files": 8.0,  "p75_files": 15, "count": 0},
    "ui_change": {"avg_files": 4.0, "p75_files": 7,  "count": 0},
    "other":    {"avg_files": 5.0,  "p75_files": 9,  "count": 0},
}


# ── Cache helpers ─────────────────────────────────────────────────────────────
# We use module-level caches rather than @lru_cache so they can be cleared
# if the knowledge base is rebuilt during the same process lifetime.

_summaries_cache: list[dict] | None = None
_co_change_cache: dict[str, int] | None = None
_scope_baselines_cache: dict[str, dict] | None = None


def _clear_cache() -> None:
    """Clear all in-memory caches (call after a rebuild)."""
    global _summaries_cache, _co_change_cache, _scope_baselines_cache
    _summaries_cache = None
    _co_change_cache = None
    _scope_baselines_cache = None


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_summaries() -> list[dict]:
    """
    Load file_summaries.json.
    Returns an empty list if the file does not exist or cannot be parsed.
    """
    global _summaries_cache
    if _summaries_cache is not None:
        return _summaries_cache

    if not _SUMMARIES_FILE.exists():
        return []

    try:
        with open(_SUMMARIES_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            _summaries_cache = [s for s in data if isinstance(s, dict)]
        else:
            _summaries_cache = []
    except Exception as exc:
        print(f"[knowledge_base] WARNING: could not load summaries: {exc}")
        _summaries_cache = []

    return _summaries_cache


def load_repo_map() -> str:
    """
    Load repo_map.txt.
    Returns an empty string if the file does not exist.
    """
    if not _REPO_MAP_FILE.exists():
        return ""
    try:
        return _REPO_MAP_FILE.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[knowledge_base] WARNING: could not load repo map: {exc}")
        return ""


def _load_co_change() -> dict[str, int]:
    global _co_change_cache
    if _co_change_cache is not None:
        return _co_change_cache

    if not _CO_CHANGE_FILE.exists():
        _co_change_cache = {}
        return _co_change_cache

    try:
        with open(_CO_CHANGE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _co_change_cache = {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception as exc:
        print(f"[knowledge_base] WARNING: could not load co_change: {exc}")
        _co_change_cache = {}

    return _co_change_cache


def _load_scope_baselines() -> dict[str, dict]:
    global _scope_baselines_cache
    if _scope_baselines_cache is not None:
        return _scope_baselines_cache

    if not _SCOPE_BASELINES_FILE.exists():
        _scope_baselines_cache = dict(_DEFAULT_BASELINES)
        return _scope_baselines_cache

    try:
        with open(_SCOPE_BASELINES_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _scope_baselines_cache = data if isinstance(data, dict) else dict(_DEFAULT_BASELINES)
    except Exception as exc:
        print(f"[knowledge_base] WARNING: could not load scope baselines: {exc}")
        _scope_baselines_cache = dict(_DEFAULT_BASELINES)

    return _scope_baselines_cache


# ── Module map ────────────────────────────────────────────────────────────────

def load_module_map() -> dict[str, dict]:
    """
    Build a module → metadata map from summaries.

    Returns:
        {
          "banking": {
              "file_count": 47,
              "files": ["app/banking/...", ...],
              "domain_concepts": ["bank-account", ...]  # deduplicated, sorted
          },
          ...
        }
    """
    summaries = load_summaries()
    modules: dict[str, dict] = defaultdict(lambda: {"file_count": 0, "files": [], "domain_concepts": []})
    concept_sets: dict[str, set] = defaultdict(set)

    for s in summaries:
        mod = s.get("module", "other") or "other"
        modules[mod]["file_count"] += 1
        modules[mod]["files"].append(s.get("path", ""))
        for concept in s.get("domain_concepts", []):
            concept_sets[mod].add(concept)

    # Attach sorted concept lists
    for mod in modules:
        modules[mod]["domain_concepts"] = sorted(concept_sets[mod])

    return dict(modules)


# ── Query functions ───────────────────────────────────────────────────────────

def get_files_for_module(module: str) -> list[dict]:
    """
    Return all file summaries whose module field equals the given value.
    Case-insensitive match.
    """
    module_lower = module.lower()
    return [s for s in load_summaries() if (s.get("module") or "").lower() == module_lower]


def search_by_concepts(concepts: list[str], top_k: int = 20) -> list[dict]:
    """
    Find files whose domain_concepts overlap with the given concepts.

    Scoring: number of matching concepts (case-insensitive).
    Returns top_k results sorted by score descending.
    """
    if not concepts:
        return []

    needle_set = {c.lower() for c in concepts}
    scored: list[tuple[int, dict]] = []

    for summary in load_summaries():
        file_concepts = {c.lower() for c in summary.get("domain_concepts", [])}
        score = len(needle_set & file_concepts)
        if score > 0:
            scored.append((score, summary))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]


def get_file_summary(path: str) -> dict | None:
    """
    Get summary for a specific file path (relative POSIX path).
    Returns None if not found.
    """
    # Normalize to forward slashes for consistent lookup
    normalized = path.replace("\\", "/")
    for summary in load_summaries():
        if summary.get("path", "").replace("\\", "/") == normalized:
            return summary
    return None


def get_related_files(path: str, top_k: int = 5) -> list[str]:
    """
    Use co-change data to find files that commonly change alongside the given file.

    Returns a list of file paths sorted by co-change frequency (descending).
    """
    co_change = _load_co_change()
    normalized = path.replace("\\", "/")
    matches: list[tuple[int, str]] = []

    for pair_key, count in co_change.items():
        file_a, sep, file_b = pair_key.partition("|")
        if not sep:
            continue
        file_a = file_a.replace("\\", "/")
        file_b = file_b.replace("\\", "/")

        if file_a == normalized:
            matches.append((count, file_b))
        elif file_b == normalized:
            matches.append((count, file_a))

    matches.sort(key=lambda x: x[0], reverse=True)
    return [fp for _, fp in matches[:top_k]]


def get_scope_baseline(ticket_type: str) -> dict:
    """
    Return scope baseline statistics for a commit/ticket type.

    Parameters
    ----------
    ticket_type : str
        One of "bug_fix", "feature", "ui_change", "refactor", "other".

    Returns
    -------
    dict
        {"avg_files": float, "p75_files": float, "count": int}
    """
    baselines = _load_scope_baselines()
    # Normalize common aliases
    normalized = ticket_type.lower().replace("-", "_").replace(" ", "_")
    alias_map = {
        "bug": "bug_fix",
        "fix": "bug_fix",
        "hotfix": "bug_fix",
        "feat": "feature",
        "new": "feature",
        "style": "ui_change",
        "ui": "ui_change",
        "design": "ui_change",
        "clean": "refactor",
        "rename": "refactor",
    }
    normalized = alias_map.get(normalized, normalized)

    if normalized in baselines:
        return dict(baselines[normalized])

    # Fall back to "other"
    return dict(baselines.get("other", _DEFAULT_BASELINES["other"]))


def get_summaries_for_context(
    concepts: list[str],
    module: str | None,
    max_tokens: int = 6000,
) -> str:
    """
    Assemble a context block of file summaries for injection into agent prompts.

    Finds relevant files by concept overlap (and optionally module filter),
    then formats them compactly into a string that fits within max_tokens
    (approximated as max_tokens * 4 characters).

    Returns a ready-to-inject string.
    """
    max_chars = max_tokens * 4

    # Gather candidates: concept-matched first, then module-filtered
    concept_matches = search_by_concepts(concepts, top_k=30) if concepts else []
    concept_paths = {s["path"] for s in concept_matches}

    # Also include module files not already in concept_matches
    module_matches: list[dict] = []
    if module:
        for s in get_files_for_module(module):
            if s["path"] not in concept_paths:
                module_matches.append(s)

    # Merge: concept matches first (they're more targeted), then module fillers
    candidates = concept_matches + module_matches

    if not candidates:
        return ""

    # Format each summary compactly
    lines: list[str] = ["## Relevant File Summaries"]
    chars_used = len(lines[0]) + 1

    for summary in candidates:
        block = _format_summary_compact(summary)
        block_len = len(block) + 1
        if chars_used + block_len > max_chars:
            break
        lines.append(block)
        chars_used += block_len

    return "\n".join(lines)


def _format_summary_compact(s: dict) -> str:
    """Format a single file summary as a compact multi-line block."""
    parts: list[str] = [f"### {s.get('path', '?')}"]

    purpose = s.get("purpose", "")
    if purpose and not purpose.startswith("[LLM error"):
        parts.append(f"Purpose: {purpose}")

    module = s.get("module", "")
    complexity = s.get("complexity", "")
    if module or complexity:
        parts.append(f"Module: {module}  Complexity: {complexity}")

    key_exports = s.get("key_exports", [])
    if key_exports:
        parts.append(f"Exports: {', '.join(key_exports)}")

    domain_concepts = s.get("domain_concepts", [])
    if domain_concepts:
        parts.append(f"Concepts: {', '.join(domain_concepts)}")

    api_routes = s.get("api_routes", [])
    if api_routes:
        parts.append(f"Routes: {', '.join(api_routes)}")

    prisma_models = s.get("prisma_models", [])
    if prisma_models:
        parts.append(f"Models: {', '.join(prisma_models)}")

    return "\n".join(parts)
