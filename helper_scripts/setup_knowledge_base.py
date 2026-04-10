#!/usr/bin/env python3
# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
setup_knowledge_base.py
========================
Master orchestrator for building all code intelligence knowledge base files.

Run this once after a fresh clone, or after updating file_summaries.json.

Steps:
  1. Build repo_map.txt from file_summaries.json (fast, no API calls)
  2. Extract module_keywords.json from file_summaries.json (fast, no API calls)
  3. Validate everything looks correct

Prerequisites:
  • code_intelligence/data/file_summaries.json must exist
    (run helper_scripts/rebuild_file_summaries_simple.py --resume --yes first)

Usage:
  python helper_scripts/setup_knowledge_base.py
  python helper_scripts/setup_knowledge_base.py --skip-validate
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "code_intelligence" / "data"
SCRIPTS_DIR = ROOT / "helper_scripts"

PYTHON = sys.executable


def run(script: str, label: str) -> bool:
    """Run a helper script, return True on success."""
    path = SCRIPTS_DIR / script
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Running: {path.name}")
    print(f"{'='*60}")
    result = subprocess.run([PYTHON, str(path)], cwd=str(ROOT))
    if result.returncode != 0:
        print(f"  FAIL FAILED (exit code {result.returncode})")
        return False
    print(f"  OK Done")
    return True


def main() -> int:
    skip_validate = "--skip-validate" in sys.argv

    print("\n" + "=" * 60)
    print("  Artoo — Knowledge Base Setup")
    print("=" * 60)

    # Check prerequisite
    summaries_file = DATA_DIR / "file_summaries.json"
    if not summaries_file.exists():
        print(f"\nFAIL Prerequisite missing: {summaries_file}")
        print("   Run: python helper_scripts/rebuild_file_summaries_simple.py --resume --yes")
        return 1

    import json
    with open(summaries_file) as f:
        count = len(json.load(f))
    print(f"\nOK file_summaries.json found: {count} entries")

    if count < 500:
        print(f"  WARNING Warning: only {count} entries — rebuild may be incomplete")

    # Step 1: Build repo_map.txt
    ok = run("build_repo_map_from_summaries.py", "Step 1/2 — Build repo_map.txt")
    if not ok:
        print("  Repo map build failed — continuing anyway")

    # Step 2: Build module_keywords.json
    ok = run("build_module_keywords.py", "Step 2/2 — Build module_keywords.json")
    if not ok:
        print("  Module keywords build failed — pipeline will use hardcoded fallback")

    # Step 3: Validate
    if not skip_validate:
        print(f"\n{'='*60}")
        print("  Step 3/3 — Validate Knowledge Base")
        print(f"{'='*60}")
        result = subprocess.run(
            [PYTHON, str(SCRIPTS_DIR / "validate_knowledge_base.py")],
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            print("\nFAIL Validation found issues — review output above")
            return 1

    print(f"\n{'='*60}")
    print("  SUCCESS  Knowledge base setup complete!")
    print("  The pipeline will now use the full KB for context assembly.")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
