#!/usr/bin/env python3
# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
build_co_change.py
==================
Builds co_change.json from GitHub commit history using GitHub REST API.

Output format:
{
  "file1.ts|file2.ts": 15,  // changed together in 15 commits
  "file1.ts|file3.ts": 8,
  ...
}

This file is used by:
- Explorer agent (selects files that co-change with identified files)
- Code proposal agent (suggests files that might need changes)
- Context assembler (finds related files)
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings

OUTPUT_FILE = Path(__file__).parent.parent / "code_intelligence" / "data" / "co_change.json"

SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".sql", ".prisma"}

# GitHub REST API base
API_BASE = "https://api.github.com"


def get_github_headers():
    token = settings.github_personal_access_token.get_secret_value()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_commits(owner: str, repo: str, max_commits: int = 500) -> list[dict]:
    """Fetch recent commits using GitHub REST API with pagination."""
    headers = get_github_headers()
    commits = []
    page = 1
    per_page = 100

    while len(commits) < max_commits:
        url = f"{API_BASE}/repos/{owner}/{repo}/commits"
        params = {"per_page": per_page, "page": page}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        commits.extend(batch)
        page += 1

        if len(batch) < per_page:
            break

        # Rate limit: stay under 5000 req/hr
        time.sleep(0.2)

    return commits[:max_commits]


def fetch_commit_files(owner: str, repo: str, sha: str) -> list[str]:
    """Fetch files changed in a single commit via REST API."""
    headers = get_github_headers()
    url = f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}"
    resp = requests.get(url, headers=headers, timeout=30)

    if resp.status_code != 200:
        return []

    data = resp.json()
    files = []
    for f in data.get("files", []):
        filename = f.get("filename", "")
        if filename and Path(filename).suffix.lower() in SOURCE_EXTS:
            files.append(filename)

    return files


def build():
    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    print("=" * 70)
    print(f"Building co_change.json from {owner}/{repo} commit history")
    print("=" * 70)

    # Step 1: Fetch commits
    print(f"\nFetching commits...")
    commits = fetch_commits(owner, repo, max_commits=500)
    print(f"  Got {len(commits)} commits")

    if not commits:
        print("ERROR: No commits found. Check GitHub token and repo access.")
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump({}, f)
        return

    # Step 2: For each commit, get changed files
    print(f"\nAnalyzing commits for co-change patterns...")
    co_change: dict[str, int] = defaultdict(int)
    processed = 0
    skipped = 0

    for commit in commits:
        sha = commit.get("sha", "")
        if not sha:
            continue

        files = fetch_commit_files(owner, repo, sha)
        processed += 1

        # Skip mega-commits (merges, refactors) — too noisy
        if len(files) > 30:
            skipped += 1
            continue

        # Count co-changes for each pair
        if len(files) >= 2:
            for i, file_a in enumerate(files):
                for file_b in files[i + 1:]:
                    pair = "|".join(sorted([file_a, file_b]))
                    co_change[pair] += 1

        if processed % 50 == 0:
            print(f"  Processed {processed}/{len(commits)} commits ({len(co_change)} pairs so far)...")

        # Rate limit
        time.sleep(0.15)

    print(f"  Processed {processed} commits, skipped {skipped} mega-commits")

    # Step 3: Filter noise — keep pairs with 2+ co-changes
    filtered = {k: v for k, v in co_change.items() if v >= 2}

    # Sort by count descending
    sorted_map = dict(sorted(filtered.items(), key=lambda x: -x[1]))

    # Step 4: Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_map, f, indent=2)

    size = OUTPUT_FILE.stat().st_size

    print(f"\n{'=' * 70}")
    print(f"  Output: {OUTPUT_FILE}")
    print(f"  Size: {size:,} bytes ({size / 1024:.1f} KB)")
    print(f"  Total pairs (2+ co-changes): {len(sorted_map)}")
    print(f"{'=' * 70}")

    if sorted_map:
        top = list(sorted_map.items())[:15]
        print("\nTop 15 co-change pairs:")
        for pair_key, count in top:
            a, b = pair_key.split("|")
            print(f"  {count:3d}x  {a}")
            print(f"        + {b}")
        print()


if __name__ == "__main__":
    build()
