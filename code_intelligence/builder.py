# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
"""
builder.py — Orchestrator that builds the complete code intelligence knowledge
base in the correct order.

Usage:
    python -m code_intelligence.builder           # incremental build
    python -m code_intelligence.builder --force   # full rebuild
"""

import argparse
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_DATA_DIR = _THIS_DIR / "data"

_REPO_MAP_FILE = _DATA_DIR / "repo_map.txt"
_CO_CHANGE_FILE = _DATA_DIR / "co_change.json"
_SCOPE_BASELINES_FILE = _DATA_DIR / "scope_baselines.json"
_SUMMARIES_FILE = _DATA_DIR / "file_summaries.json"


# ── Status helpers ────────────────────────────────────────────────────────────

def get_build_status() -> dict:
    """
    Return a status dict describing which knowledge-base components exist.

    Returns
    -------
    dict
        {
          "repo_map": bool,
          "git_analysis": bool,      # True only if BOTH co_change + baselines exist
          "file_summaries": int,     # number of summaries present (0 if not built)
        }
    """
    file_summaries_count = 0
    if _SUMMARIES_FILE.exists():
        try:
            import json
            with open(_SUMMARIES_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            file_summaries_count = len(data) if isinstance(data, list) else 0
        except Exception:
            file_summaries_count = -1  # file exists but is corrupt

    return {
        "repo_map": _REPO_MAP_FILE.exists(),
        "git_analysis": _CO_CHANGE_FILE.exists() and _SCOPE_BASELINES_FILE.exists(),
        "file_summaries": file_summaries_count,
    }


def is_built() -> bool:
    """
    Return True if all knowledge-base files exist and file_summaries.json is
    non-empty. Does NOT guarantee the content is up-to-date.
    """
    status = get_build_status()
    return (
        status["repo_map"]
        and status["git_analysis"]
        and status["file_summaries"] > 0
    )


# ── Main builder ──────────────────────────────────────────────────────────────

def build_all(force_rebuild: bool = False) -> None:
    """
    Build the complete code intelligence knowledge base.

    Steps (in order):
      1. Repo map  — fast, pure-regex, no LLM
      2. Git analysis — fast, subprocess git log
      3. File summaries — slow, LLM-powered (skips already-done unless force)

    Parameters
    ----------
    force_rebuild : bool
        If True, regenerate every component from scratch even if it already
        exists.  If False (default), skip completed steps.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    status = get_build_status()
    print("=" * 60)
    print("IDRE Code Intelligence — Build")
    print(f"  force_rebuild = {force_rebuild}")
    print(f"  repo_map      : {'exists' if status['repo_map'] else 'missing'}")
    print(f"  git_analysis  : {'exists' if status['git_analysis'] else 'missing'}")
    print(f"  file_summaries: {status['file_summaries']} entries")
    print("=" * 60)

    t_start = time.time()

    # ── Step 1: Repo map ──────────────────────────────────────────────────────
    if force_rebuild or not status["repo_map"]:
        print("\n[Step 1/3] Building repo map…")
        from code_intelligence.repo_map import build_repo_map
        text = build_repo_map()
        print(f"  Repo map: {len(text):,} chars")
    else:
        print("\n[Step 1/3] Repo map already exists — skipping (use --force to rebuild)")

    # ── Step 2: Git analysis ──────────────────────────────────────────────────
    if force_rebuild or not status["git_analysis"]:
        print("\n[Step 2/3] Running git analysis…")
        from code_intelligence.git_analyzer import build_git_analysis
        stats = build_git_analysis()
        print(f"  Commits analyzed : {stats.get('commits_analyzed', 0)}")
        print(f"  Unique files     : {stats.get('unique_files', 0)}")
        print(f"  Co-change pairs  : {stats.get('co_change_pairs', 0)}")
    else:
        print("\n[Step 2/3] Git analysis already exists — skipping (use --force to rebuild)")

    # ── Step 3: File summaries (LLM) ─────────────────────────────────────────
    print("\n[Step 3/3] Building file summaries (LLM)…")
    print("  This step is incremental: already-summarized files are skipped.")
    print("  Pass force_rebuild=True to re-summarize everything.\n")
    from code_intelligence.file_summarizer import build_file_summaries
    out_path = build_file_summaries(force_rebuild=force_rebuild)
    print(f"  Summaries saved to: {out_path}")

    elapsed = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f"Build complete in {elapsed:.1f}s")

    # Clear the in-memory cache so updated files are picked up immediately
    try:
        from code_intelligence import knowledge_base
        knowledge_base._clear_cache()
    except Exception:
        pass

    final_status = get_build_status()
    print(f"  repo_map      : {'OK' if final_status['repo_map'] else 'MISSING'}")
    print(f"  git_analysis  : {'OK' if final_status['git_analysis'] else 'MISSING'}")
    print(f"  file_summaries: {final_status['file_summaries']} entries")
    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build the IDRE code intelligence knowledge base."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild of all components (including already-summarized files).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print build status and exit without building.",
    )
    args = parser.parse_args()

    if args.status:
        import json as _json
        print(_json.dumps(get_build_status(), indent=2))
    else:
        build_all(force_rebuild=args.force)
