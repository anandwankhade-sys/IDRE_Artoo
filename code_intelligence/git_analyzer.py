# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
"""
git_analyzer.py — Analyze git history of idre-codebase/ to build co-change
patterns and scope baselines used by the impact-assessment agents.

Outputs:
  data/co_change.json     — top-500 file-pair co-change counts
  data/scope_baselines.json — avg/p75 files changed per commit type
"""

import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_HYBRID_DIR = _THIS_DIR.parent
_CODEBASE_DIR = _HYBRID_DIR / "idre-codebase"
_DATA_DIR = _THIS_DIR / "data"
_CO_CHANGE_FILE = _DATA_DIR / "co_change.json"
_SCOPE_BASELINES_FILE = _DATA_DIR / "scope_baselines.json"

# ── Configuration ────────────────────────────────────────────────────────────

GIT_LOG_LIMIT = 200        # Last N commits to analyze
TOP_PAIRS = 500            # Top file pairs to keep in co_change.json

# Commit-type keyword mapping (checked against lowercase commit message)
_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("bug_fix",  ["fix", "bug", "hotfix", "patch", "correct", "repair"]),
    ("feature",  ["feat", "feature", "add", "implement", "new", "support"]),
    ("refactor", ["refactor", "clean", "rename", "reorganize", "restructure", "move"]),
    ("ui_change", ["style", "ui", "design", "css", "tailwind", "layout", "theme"]),
]

_SAFE_DEFAULTS: dict[str, dict] = {
    "bug_fix":  {"avg_files": 3.0,  "p75_files": 5,  "count": 0},
    "feature":  {"avg_files": 7.0,  "p75_files": 12, "count": 0},
    "refactor": {"avg_files": 8.0,  "p75_files": 15, "count": 0},
    "ui_change": {"avg_files": 4.0, "p75_files": 7,  "count": 0},
    "other":    {"avg_files": 5.0,  "p75_files": 9,  "count": 0},
}


# ── Git log parser ────────────────────────────────────────────────────────────

def _run_git_log(codebase_dir: Path) -> str:
    """Run git log --numstat and return raw output."""
    cmd = [
        "git", "-C", str(codebase_dir),
        "log", "--numstat",
        f"--pretty=format:COMMIT:%H %s",
        f"-n", str(GIT_LOG_LIMIT),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {result.stderr.strip()}")
    return result.stdout


def _parse_git_log(raw: str) -> list[dict[str, Any]]:
    """
    Parse raw git log output into a list of commit dicts:
      [{"hash": "...", "message": "...", "files": ["path1", ...]}, ...]
    """
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in raw.splitlines():
        if line.startswith("COMMIT:"):
            if current is not None:
                commits.append(current)
            rest = line[7:].strip()  # remove "COMMIT:"
            parts = rest.split(" ", 1)
            commit_hash = parts[0]
            message = parts[1] if len(parts) > 1 else ""
            current = {"hash": commit_hash, "message": message, "files": []}
            continue

        if current is None:
            continue

        # numstat lines: "additions\tdeletions\tpath"
        # Binary files show "-\t-\tpath"
        parts = line.split("\t")
        if len(parts) == 3:
            file_path = parts[2].strip()
            # Skip renames shown as "old => new" in braces; extract real path
            if "{" in file_path and "=>" in file_path:
                # e.g. "src/{old => new}/file.ts" → take the "new" side
                file_path = re.sub(
                    r"\{[^}]*=>\s*([^}]*)\}",
                    lambda m: m.group(1).strip(),
                    file_path,
                )
            # Skip binary nulls and empty
            if file_path:
                current["files"].append(file_path)

    if current is not None:
        commits.append(current)

    return commits


# ── Commit type classifier ────────────────────────────────────────────────────

def _classify_commit(message: str) -> str:
    """Return one of: bug_fix, feature, refactor, ui_change, other."""
    msg_lower = message.lower()
    for commit_type, keywords in _TYPE_KEYWORDS:
        if any(kw in msg_lower for kw in keywords):
            return commit_type
    return "other"


# ── Co-change matrix ──────────────────────────────────────────────────────────

def _build_co_change(commits: list[dict]) -> dict[str, int]:
    """
    Build a co-change frequency dict.
    Key: "fileA|fileB" (alphabetically sorted), Value: co-occurrence count.
    Returns top TOP_PAIRS pairs.
    """
    counts: dict[str, int] = defaultdict(int)

    for commit in commits:
        files = sorted(set(commit["files"]))
        # Only count pairs (skip commits with 0 or 1 file)
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                key = f"{files[i]}|{files[j]}"
                counts[key] += 1

    # Keep only top N pairs by count
    sorted_pairs = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_pairs[:TOP_PAIRS])


# ── Scope baselines ───────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], p: float) -> float:
    """Compute the p-th percentile (0–100) of a sorted list."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = (p / 100) * (n - 1)
    lower = int(idx)
    upper = min(lower + 1, n - 1)
    frac = idx - lower
    return sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac


def _build_scope_baselines(commits: list[dict]) -> dict[str, dict]:
    """
    Group commits by type and compute avg + p75 files changed.
    Returns dict matching _SAFE_DEFAULTS structure.
    """
    buckets: dict[str, list[int]] = {
        "bug_fix": [], "feature": [], "refactor": [], "ui_change": [], "other": []
    }

    for commit in commits:
        commit_type = _classify_commit(commit["message"])
        n_files = len(commit["files"])
        if n_files > 0:
            buckets[commit_type].append(n_files)

    result: dict[str, dict] = {}
    for commit_type, values in buckets.items():
        if not values:
            result[commit_type] = dict(_SAFE_DEFAULTS[commit_type])
            continue
        values_sorted = sorted(values)
        avg = sum(values_sorted) / len(values_sorted)
        p75 = _percentile(values_sorted, 75)
        result[commit_type] = {
            "avg_files": round(avg, 2),
            "p75_files": round(p75, 1),
            "count": len(values_sorted),
        }

    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def build_git_analysis(codebase_dir: Path | None = None) -> dict[str, Any]:
    """
    Analyze git history and write co_change.json + scope_baselines.json.

    Returns a summary dict with:
      {
        "commits_analyzed": int,
        "unique_files": int,
        "co_change_pairs": int,
        "scope_baselines": {...},
      }
    """
    if codebase_dir is None:
        codebase_dir = _CODEBASE_DIR

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Attempt git log ───────────────────────────────────────────────────────
    try:
        print(f"[git_analyzer] Running git log on {codebase_dir} (last {GIT_LOG_LIMIT} commits)…")
        raw = _run_git_log(codebase_dir)
    except Exception as exc:
        print(f"[git_analyzer] WARNING: git log failed ({exc}). Using safe defaults.")
        _write_defaults()
        return {
            "commits_analyzed": 0,
            "unique_files": 0,
            "co_change_pairs": 0,
            "scope_baselines": dict(_SAFE_DEFAULTS),
            "warning": str(exc),
        }

    commits = _parse_git_log(raw)
    if not commits:
        print("[git_analyzer] WARNING: No commits parsed. Using safe defaults.")
        _write_defaults()
        return {
            "commits_analyzed": 0,
            "unique_files": 0,
            "co_change_pairs": 0,
            "scope_baselines": dict(_SAFE_DEFAULTS),
            "warning": "no commits found",
        }

    print(f"[git_analyzer] Parsed {len(commits)} commits.")

    # ── Co-change analysis ────────────────────────────────────────────────────
    co_change = _build_co_change(commits)
    unique_files: set[str] = set()
    for commit in commits:
        unique_files.update(commit["files"])

    # ── Scope baselines ───────────────────────────────────────────────────────
    scope_baselines = _build_scope_baselines(commits)

    # ── Write outputs ─────────────────────────────────────────────────────────
    with open(_CO_CHANGE_FILE, "w", encoding="utf-8") as fh:
        json.dump(co_change, fh, indent=2)
    print(f"[git_analyzer] co_change.json: {len(co_change)} pairs → {_CO_CHANGE_FILE}")

    with open(_SCOPE_BASELINES_FILE, "w", encoding="utf-8") as fh:
        json.dump(scope_baselines, fh, indent=2)
    print(f"[git_analyzer] scope_baselines.json → {_SCOPE_BASELINES_FILE}")

    return {
        "commits_analyzed": len(commits),
        "unique_files": len(unique_files),
        "co_change_pairs": len(co_change),
        "scope_baselines": scope_baselines,
    }


def _write_defaults() -> None:
    """Write safe-default files when git analysis cannot run."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CO_CHANGE_FILE, "w", encoding="utf-8") as fh:
        json.dump({}, fh)
    with open(_SCOPE_BASELINES_FILE, "w", encoding="utf-8") as fh:
        json.dump(_SAFE_DEFAULTS, fh, indent=2)
    print(f"[git_analyzer] Wrote safe defaults to {_DATA_DIR}")


if __name__ == "__main__":
    stats = build_git_analysis()
    print(json.dumps(stats, indent=2))
