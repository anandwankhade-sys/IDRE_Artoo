#!/usr/bin/env python3
# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
build_module_keywords.py
========================
Extract domain keywords from file_summaries.json, grouped by module.
Outputs code_intelligence/data/module_keywords.json.

This creates a data-driven keyword vocabulary from the actual codebase,
replacing the hand-written _MODULE_KEYWORDS in context_assembler.py.

Run after rebuild_file_summaries_simple.py completes.
"""

import json
from collections import defaultdict
from pathlib import Path

SUMMARIES_FILE = Path(__file__).parent.parent / "code_intelligence" / "data" / "file_summaries.json"
OUTPUT_FILE = Path(__file__).parent.parent / "code_intelligence" / "data" / "module_keywords.json"

# A concept must appear in at least this many files to be included
MIN_FREQUENCY = 2
# Cap per module to avoid bloat
MAX_KEYWORDS_PER_MODULE = 60

# Generic non-domain terms to exclude even if frequent
_GENERIC_TERMS = {
    "component", "function", "interface", "type", "class", "file", "data",
    "value", "object", "string", "number", "boolean", "array", "list",
    "page", "form", "modal", "button", "table", "item", "items", "error",
    "loading", "state", "props", "params", "result", "response", "request",
    "import", "export", "default", "null", "undefined", "none", "true", "false",
    "user", "users", "id", "name", "date", "time", "status", "type",
}


def build_module_keywords() -> dict[str, list[str]]:
    print(f"Loading {SUMMARIES_FILE.name}...")
    with open(SUMMARIES_FILE, encoding="utf-8") as f:
        summaries = json.load(f)
    print(f"Loaded {len(summaries)} file summaries")

    # Count concept frequency per module
    module_concept_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    module_file_counts: dict[str, int] = defaultdict(int)

    for s in summaries:
        if not isinstance(s, dict):
            continue
        module = (s.get("module") or "general").lower().strip()
        module_file_counts[module] += 1

        for concept in s.get("domain_concepts", []):
            if not concept or not isinstance(concept, str):
                continue
            c = concept.lower().strip()
            # Skip very short terms, generic terms, and single-char items
            if len(c) < 3 or c in _GENERIC_TERMS:
                continue
            module_concept_counts[module][c] += 1

        # Also extract meaningful terms from purpose string
        purpose = s.get("purpose", "")
        if purpose and not purpose.startswith("[LLM error") and not purpose.startswith("Empty"):
            # Extract multi-word domain phrases (rough heuristic: hyphenated or compound)
            for word in purpose.lower().split():
                word = word.strip(".,;:()")
                if len(word) >= 5 and "-" in word:
                    module_concept_counts[module][word] += 1

    # Build output: keywords sorted by frequency, filtered by MIN_FREQUENCY
    result: dict[str, list[str]] = {}
    for module in sorted(module_concept_counts.keys()):
        counts = module_concept_counts[module]
        top = sorted(
            [(count, kw) for kw, count in counts.items() if count >= MIN_FREQUENCY],
            reverse=True,
        )
        keywords = [kw for _, kw in top[:MAX_KEYWORDS_PER_MODULE]]
        if keywords:
            result[module] = keywords

    # Print summary
    print("\nModule keyword counts:")
    for mod, kws in sorted(result.items()):
        print(f"  {mod:20s}: {len(kws):3d} keywords  ({module_file_counts[mod]} files)")

    # Save
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved {OUTPUT_FILE}")
    print(f"Total modules: {len(result)}, total keywords: {sum(len(v) for v in result.values())}")
    return result


if __name__ == "__main__":
    build_module_keywords()
