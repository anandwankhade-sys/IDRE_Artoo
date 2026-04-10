#!/usr/bin/env python3
"""
Build repo_map.txt from existing file_summaries.json
This is a fast alternative that doesn't require GitHub API calls
"""

import json
import sys
from pathlib import Path

# Paths
FINAL_DIR = Path(__file__).parent.parent
SUMMARIES_FILE = FINAL_DIR / "code_intelligence" / "data" / "file_summaries.json"
OUTPUT_FILE = FINAL_DIR / "code_intelligence" / "data" / "repo_map.txt"

def main():
    print("=" * 70)
    print("Building repo_map.txt from file_summaries.json")
    print("=" * 70)

    if not SUMMARIES_FILE.exists():
        print(f"ERROR: {SUMMARIES_FILE} not found")
        return False

    # Load summaries
    print(f"\nLoading {SUMMARIES_FILE}...")
    with open(SUMMARIES_FILE, encoding="utf-8") as f:
        summaries = json.load(f)

    print(f"Found {len(summaries)} files")

    # Build repo_map
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nWriting repo_map.txt...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for summary in sorted(summaries, key=lambda x: x.get("path", "")):
            path = summary.get("path", "")
            if not path:
                continue

            f.write(f"{path}\n")

            # Add exports if available
            exports = summary.get("key_exports", [])
            for export in exports[:20]:  # Limit to 20 exports per file
                f.write(f"  {export}\n")

    file_size = OUTPUT_FILE.stat().st_size
    files_with_exports = sum(1 for s in summaries if s.get("key_exports"))
    total_exports = sum(len(s.get("key_exports", [])) for s in summaries)

    print("\n" + "=" * 70)
    print("[SUCCESS]")
    print("=" * 70)
    print(f"Output file: {OUTPUT_FILE}")
    print(f"File size: {file_size:,} bytes ({file_size / 1024:.1f} KB)")
    print(f"Total files: {len(summaries)}")
    print(f"Files with exports: {files_with_exports}")
    print(f"Total exports: {total_exports}")
    print("=" * 70)

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
