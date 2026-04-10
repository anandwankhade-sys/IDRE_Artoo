#!/usr/bin/env python3
# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
validate_knowledge_base.py
===========================
Validates the code intelligence knowledge base files.
Prints a health report and exits 0 if healthy, 1 if critical issues found.

Run after setup_knowledge_base.py or any manual rebuild step.
"""

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "code_intelligence" / "data"

CHECKS = {
    "file_summaries.json": {"min_entries": 1000, "type": "list"},
    "repo_map.txt":        {"min_bytes": 10_000},
    "module_keywords.json":{"min_modules": 3, "type": "dict"},
    "scope_baselines.json":{"type": "dict"},
    "co_change.json":      {"type": "dict", "optional": True},
}


def validate() -> bool:
    print("=" * 60)
    print("Knowledge Base Validation")
    print("=" * 60)

    all_ok = True
    critical_failures: list[str] = []

    for filename, rules in CHECKS.items():
        path = DATA_DIR / filename
        optional = rules.get("optional", False)
        prefix = "  [OPT]" if optional else "  [REQ]"

        if not path.exists():
            if optional:
                print(f"{prefix} {filename}: NOT FOUND (optional — skip)")
                continue
            print(f"{prefix} {filename}: MISSING FAIL")
            critical_failures.append(f"{filename} not found")
            all_ok = False
            continue

        size = path.stat().st_size
        print(f"{prefix} {filename}: {size:,} bytes", end="")

        # Min bytes check
        min_bytes = rules.get("min_bytes")
        if min_bytes and size < min_bytes:
            print(f" — TOO SMALL (need >={min_bytes:,} bytes) FAIL")
            critical_failures.append(f"{filename} too small")
            all_ok = False
            continue

        # JSON content checks
        data_type = rules.get("type")
        if data_type:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f" — JSON PARSE ERROR: {e} FAIL")
                critical_failures.append(f"{filename} parse error")
                all_ok = False
                continue

            if data_type == "list":
                count = len(data)
                min_entries = rules.get("min_entries", 0)
                if count < min_entries:
                    print(f" — {count} entries (need >={min_entries}) FAIL")
                    critical_failures.append(f"{filename} only {count} entries")
                    all_ok = False
                else:
                    print(f" — {count} entries OK")

                    # Print module breakdown for file_summaries
                    if filename == "file_summaries.json":
                        _print_module_breakdown(data)

            elif data_type == "dict":
                count = len(data)
                min_modules = rules.get("min_modules", 0)
                if count < min_modules:
                    print(f" — {count} keys (need >={min_modules}) FAIL")
                    critical_failures.append(f"{filename} insufficient keys")
                    all_ok = False
                else:
                    print(f" — {count} keys OK")

                    if filename == "module_keywords.json":
                        _print_keyword_breakdown(data)
        else:
            print(" OK")

    # Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("SUCCESS  Knowledge base is HEALTHY — all checks passed")
    else:
        print("FAIL  Knowledge base has ISSUES:")
        for f in critical_failures:
            print(f"    • {f}")

    print("=" * 60)
    return all_ok


def _print_module_breakdown(summaries: list) -> None:
    from collections import Counter
    modules = Counter((s.get("module") or "general") for s in summaries if isinstance(s, dict))
    print("      Module breakdown:")
    for mod, count in sorted(modules.items(), key=lambda x: -x[1])[:10]:
        bar = "#" * (count // 20)
        print(f"        {mod:20s}: {count:4d}  {bar}")
    total = sum(modules.values())
    unclassified = modules.get("general", 0) + modules.get("other", 0)
    print(f"        {'TOTAL':20s}: {total:4d}  (unclassified: {unclassified})")


def _print_keyword_breakdown(keywords: dict) -> None:
    print("      Keyword counts per module:")
    for mod, kws in sorted(keywords.items()):
        print(f"        {mod:20s}: {len(kws):3d} keywords")


if __name__ == "__main__":
    ok = validate()
    sys.exit(0 if ok else 1)
