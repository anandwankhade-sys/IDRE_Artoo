# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
utils/file_index.py
===================
Builds and caches a flat set of real file paths from the local
idre-codebase/ directory.  Used to:
  1. Inject a directory tree into every LLM prompt (grounding)
  2. Validate proposed file paths post code-proposal (hallucination filter)

The index is built once per process and held in memory.
"""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

# Supported source extensions — same as benchmark count
_SOURCE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".py",
    ".sql", ".prisma", ".md", ".json",
}

# Root of the local codebase clone (relative to this file's package root)
_CODEBASE_ROOT = Path(__file__).parent.parent / "idre-codebase"


@lru_cache(maxsize=1)
def get_file_index() -> frozenset[str]:
    """
    Return a frozenset of all relative file paths in idre-codebase/
    that match _SOURCE_EXTS.  Paths use forward slashes.
    Result is cached for the life of the process.

    If local codebase is not available, falls back to reading from
    code_intelligence/data/repo_map.txt.
    """
    if _CODEBASE_ROOT.exists():
        # Prefer local codebase if available
        paths: set[str] = set()
        for root, dirs, files in os.walk(_CODEBASE_ROOT):
            # Skip hidden dirs / node_modules / .next
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in ("node_modules", ".next", "__pycache__")
            ]
            for fname in files:
                if Path(fname).suffix.lower() in _SOURCE_EXTS:
                    full = Path(root) / fname
                    rel = full.relative_to(_CODEBASE_ROOT)
                    paths.add(rel.as_posix())

        return frozenset(paths)

    # Fallback: Read from repo_map.txt
    repo_map_path = Path(__file__).parent.parent / "code_intelligence" / "data" / "repo_map.txt"
    if repo_map_path.exists():
        paths: set[str] = set()
        try:
            with open(repo_map_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    # File paths don't have leading whitespace, symbols do
                    if line and not line.startswith(" "):
                        # Verify it's a source file
                        if Path(line).suffix.lower() in _SOURCE_EXTS:
                            paths.add(line)
            return frozenset(paths)
        except Exception:
            pass

    return frozenset()


@lru_cache(maxsize=1)
def get_file_index_text(max_lines: int = 1200) -> str:
    """
    Return a compact, sorted, newline-separated listing of all real paths.
    Truncated to max_lines to stay within prompt budgets.
    Used for injection into planner + code-proposal prompts.
    """
    idx = sorted(get_file_index())
    if not idx:
        return "(file index unavailable — idre-codebase/ not found)"

    lines = idx[:max_lines]
    trailer = f"\n... ({len(idx) - max_lines} more files not shown)" if len(idx) > max_lines else ""
    return "\n".join(lines) + trailer


def get_basename_index() -> dict[str, list[str]]:
    """
    Return a dict mapping basename → [list of full relative paths].
    Used for fuzzy matching in the file validator.
    Example: {"server.ts": ["lib/auth/server.ts", "app/server.ts"]}
    """
    bmap: dict[str, list[str]] = {}
    for path in get_file_index():
        base = Path(path).name.lower()
        bmap.setdefault(base, []).append(path)
    return bmap


def _best_directory_match(proposed: str, candidates: list[str]) -> str:
    """Pick the candidate whose directory path is most similar to the proposed path."""
    proposed_parts = proposed.replace("\\", "/").lower().split("/")
    best_score = -1
    best = candidates[0]
    for c in candidates:
        c_parts = c.lower().split("/")
        # Count shared path segments
        score = sum(1 for a, b in zip(reversed(proposed_parts), reversed(c_parts)) if a == b)
        if score > best_score:
            best_score = score
            best = c
    return best


def _partial_basename_match(base: str, bmap: dict[str, list[str]]) -> str | None:
    """
    Try partial basename matching for common patterns:
    - "payment-ledger-v2.tsx" → "payment-ledger.tsx" (version suffix)
    - "administrative-closure-modal.tsx" → "case-closure-action-modal.tsx" (keyword overlap)
    """
    # Remove common suffixes like -v2, -v3, -new, -fixed
    import re
    stripped = re.sub(r'-(v\d+|new|fixed|updated|old)', '', base)
    if stripped != base and stripped in bmap:
        return bmap[stripped][0]

    # Try matching by the most distinctive part of the filename
    # e.g. "closure-modal" in "administrative-closure-modal.tsx"
    base_stem = Path(base).stem  # remove extension
    base_ext = Path(base).suffix
    for idx_base, paths in bmap.items():
        if Path(idx_base).suffix != base_ext:
            continue
        idx_stem = Path(idx_base).stem
        # Check if >50% of the hyphenated parts overlap
        proposed_parts = set(base_stem.split("-"))
        idx_parts = set(idx_stem.split("-"))
        if len(proposed_parts) >= 2 and len(idx_parts) >= 2:
            overlap = proposed_parts & idx_parts
            if len(overlap) >= max(2, len(proposed_parts) * 0.5):
                return paths[0]

    return None


def validate_proposed_paths(proposed: list[str]) -> dict:
    """
    Given a list of proposed file paths from the code proposal agent,
    return a dict with:
      - exact_matches:   paths that exist verbatim in the index
      - fuzzy_matches:   paths whose basename exists (but different directory)
      - hallucinated:    paths with no match at all
      - corrections:     suggested real path for each fuzzy match (first candidate)
    """
    idx = get_file_index()
    bmap = get_basename_index()

    exact: list[str] = []
    fuzzy: list[str] = []
    hallucinated: list[str] = []
    corrections: dict[str, str] = {}

    for p in proposed:
        # Normalise to forward slash, strip leading /
        norm = p.replace("\\", "/").lstrip("/")
        if norm in idx:
            exact.append(norm)
        else:
            base = Path(norm).name.lower()
            candidates = bmap.get(base, [])
            if candidates:
                fuzzy.append(norm)
                # Pick the candidate closest to the proposed path by directory similarity
                best = _best_directory_match(norm, candidates)
                corrections[norm] = best
            else:
                # Try partial basename matching (e.g. "payment-ledger-v2.tsx" → "payment-ledger.tsx")
                partial_match = _partial_basename_match(base, bmap)
                if partial_match:
                    fuzzy.append(norm)
                    corrections[norm] = partial_match
                else:
                    hallucinated.append(norm)

    return {
        "exact_matches": exact,
        "fuzzy_matches": fuzzy,
        "hallucinated": hallucinated,
        "corrections": corrections,
        "total_proposed": len(proposed),
        "hallucination_rate": len(hallucinated) / len(proposed) if proposed else 0.0,
    }
