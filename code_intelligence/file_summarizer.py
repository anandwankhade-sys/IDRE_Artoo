# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
"""
file_summarizer.py — LLM-powered per-file summarization for the IDRE codebase.

Walks idre-codebase/, sends each .ts/.tsx/.prisma file to Claude Haiku via
Bedrock, and stores structured JSON summaries to data/file_summaries.json.
Supports resume (skips already-summarized files) and batched rate limiting.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

# ── Paths ────────────────────────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_HYBRID_DIR = _THIS_DIR.parent
_CODEBASE_DIR = _HYBRID_DIR / "idre-codebase"
_ENV_FILE = _HYBRID_DIR / ".env"
_DATA_DIR = _THIS_DIR / "data"
_OUTPUT_FILE = _DATA_DIR / "file_summaries.json"

# ── Configuration ────────────────────────────────────────────────────────────

SOURCE_EXTENSIONS = {".ts", ".tsx", ".prisma", ".sql", ".js", ".mjs"}
TEST_EXTENSIONS = {".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"}
SKIP_DIRS = {"node_modules", ".next", ".git", "__pycache__", "dist", ".turbo"}
CONTENT_TRUNCATE_CHARS = 3_000
BATCH_SIZE = 10
BATCH_SLEEP_SECONDS = 1.0
DEFAULT_MODEL_ID = "us.anthropic.claude-3-5-haiku-20241022-v1:0"


# ── .env loader ──────────────────────────────────────────────────────────────

def _load_env(env_path: Path) -> dict[str, str]:
    """Parse a .env file and return a dict of key→value pairs."""
    result: dict[str, str] = {}
    if not env_path.exists():
        print(f"[file_summarizer] WARNING: .env not found at {env_path}")
        return result
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Strip surrounding quotes if present
                if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                    val = val[1:-1]
                result[key] = val
    return result


# ── Bedrock client ───────────────────────────────────────────────────────────

def _make_bedrock_client(env: dict[str, str]):
    """Create a boto3 bedrock-runtime client using credentials from env."""
    return boto3.client(
        "bedrock-runtime",
        region_name=env.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
    )


# ── File helpers ─────────────────────────────────────────────────────────────

def _is_test_file(path: Path) -> bool:
    """Return True if this file is a test/spec file."""
    name = path.name
    return any(name.endswith(suffix) for suffix in TEST_EXTENSIONS)


def _is_processable(path: Path) -> bool:
    """Return True if this file should be summarized (including test files)."""
    return path.suffix in SOURCE_EXTENSIONS


def _is_in_skip_dir(path: Path, base: Path) -> bool:
    """Return True if any component of path (relative to base) is in SKIP_DIRS."""
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    return any(part in SKIP_DIRS for part in rel.parts)


def _collect_files(codebase_dir: Path) -> list[Path]:
    """Walk codebase_dir and return all processable file paths."""
    results: list[Path] = []
    for path in sorted(codebase_dir.rglob("*")):
        if not path.is_file():
            continue
        if _is_in_skip_dir(path, codebase_dir):
            continue
        if _is_processable(path):
            results.append(path)
    return results


def _relative_path(path: Path, base: Path) -> str:
    """Return POSIX relative path string."""
    return path.relative_to(base).as_posix()


def _file_mtime(path: Path) -> str:
    """Return ISO date string of file mtime."""
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _git_change_count(path: Path, codebase_dir: Path) -> int:
    """Return number of git commits that touched this file (last 500)."""
    try:
        rel = path.relative_to(codebase_dir).as_posix()
        result = subprocess.run(
            ["git", "-C", str(codebase_dir), "log", "--oneline", "-n", "500", "--", rel],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        return len(lines)
    except Exception:
        return 0


def _change_frequency(count: int) -> str:
    if count >= 20:
        return "high"
    if count >= 5:
        return "medium"
    return "low"


# ── LLM prompt & call ────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are analyzing a file from the IDRE (dispute resolution) application.
File: {relative_path}
File type: {file_type}
Content:
{content}

Return ONLY a JSON object (no markdown) with these fields:
- module: the top-level feature area (one of: banking, organizations, cases, disputes, payments, auth, email, admin, shared, config, database, other)
- purpose: 1-2 sentence description of what this file does
- key_exports: list of main exported functions/components/classes (max 5). For SQL files, list the main tables/columns altered.
- domain_concepts: list of domain keywords this file relates to (max 8, e.g. "bank-account", "sub-organization", "inheritance", "nacha-export")
- imports_from: list of internal import paths (max 8, starting with ./ or ../ only). For SQL files, use empty list.
- api_routes: list of API route paths if this is a route handler (empty list otherwise)
- prisma_models: list of Prisma model names referenced (empty list if none)
- complexity: "low", "medium", or "high"
"""

_FILE_TYPE_LABELS = {
    ".ts": "TypeScript source",
    ".tsx": "TypeScript React component",
    ".prisma": "Prisma schema",
    ".sql": "SQL migration",
    ".js": "JavaScript source",
    ".mjs": "ES module JavaScript",
}


def _call_llm(
    client,
    model_id: str,
    relative_path: str,
    content: str,
    is_test: bool = False,
    file_suffix: str = ".ts",
) -> dict[str, Any]:
    """Call Bedrock and return parsed JSON dict. Raises on unrecoverable errors."""
    truncated = content[:CONTENT_TRUNCATE_CHARS]
    base_label = _FILE_TYPE_LABELS.get(file_suffix, "source file")
    file_type = f"{base_label} (TESTING — this is a test/spec file)" if is_test else base_label
    prompt = _PROMPT_TEMPLATE.format(
        relative_path=relative_path, content=truncated, file_type=file_type
    )

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt}],
    })

    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    raw = json.loads(response["body"].read())
    text = raw["content"][0]["text"].strip()

    # Strip markdown fences if model added them despite instruction
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    return json.loads(text)


def _minimal_summary(relative_path: str, path: Path, error: str) -> dict[str, Any]:
    """Return a bare-minimum summary when LLM fails."""
    return {
        "path": relative_path,
        "module": "other",
        "purpose": f"[LLM error: {error[:120]}]",
        "key_exports": [],
        "domain_concepts": [],
        "imports_from": [],
        "api_routes": [],
        "prisma_models": [],
        "complexity": "medium",
        "lines": 0,
        "last_changed": _file_mtime(path),
        "change_frequency": "low",
    }


def _build_summary(
    client,
    model_id: str,
    path: Path,
    codebase_dir: Path,
) -> dict[str, Any]:
    """Build a full summary dict for one file."""
    relative_path = _relative_path(path, codebase_dir)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return _minimal_summary(relative_path, path, f"read error: {exc}")

    lines = content.count("\n") + 1
    last_changed = _file_mtime(path)
    change_count = _git_change_count(path, codebase_dir)
    freq = _change_frequency(change_count)

    is_test = _is_test_file(path)
    try:
        llm_data = _call_llm(client, model_id, relative_path, content,
                             is_test=is_test, file_suffix=path.suffix)
    except Exception as exc:
        print(f"  [LLM error] {relative_path}: {exc}")
        summary = _minimal_summary(relative_path, path, str(exc))
        summary["lines"] = lines
        summary["last_changed"] = last_changed
        summary["change_frequency"] = freq
        return summary

    summary: dict[str, Any] = {
        "path": relative_path,
        "module": str(llm_data.get("module", "other")),
        "purpose": str(llm_data.get("purpose", "")),
        "key_exports": list(llm_data.get("key_exports", [])),
        "domain_concepts": list(llm_data.get("domain_concepts", [])),
        "imports_from": list(llm_data.get("imports_from", [])),
        "api_routes": list(llm_data.get("api_routes", [])),
        "prisma_models": list(llm_data.get("prisma_models", [])),
        "complexity": str(llm_data.get("complexity", "medium")),
        "is_test_file": is_test,
        "file_type": path.suffix,
        "lines": lines,
        "last_changed": last_changed,
        "change_frequency": freq,
    }
    return summary


# ── Main entry point ─────────────────────────────────────────────────────────

def build_file_summaries(force_rebuild: bool = False) -> Path:
    """
    Build file_summaries.json for all processable files in idre-codebase/.

    Parameters
    ----------
    force_rebuild : bool
        If True, re-summarize every file even if already present in the output.
        If False (default), skip files that already appear in the output.

    Returns
    -------
    Path
        Absolute path to the written file_summaries.json.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load AWS credentials from .env
    env = _load_env(_ENV_FILE)
    model_id = env.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    print(f"[file_summarizer] Using model: {model_id}")

    # Create Bedrock client
    try:
        client = _make_bedrock_client(env)
    except Exception as exc:
        print(f"[file_summarizer] ERROR creating Bedrock client: {exc}")
        raise

    # Load existing summaries for resume capability
    existing: dict[str, dict] = {}
    if _OUTPUT_FILE.exists() and not force_rebuild:
        try:
            with open(_OUTPUT_FILE, encoding="utf-8") as fh:
                loaded = json.load(fh)
                existing = {s["path"]: s for s in loaded if isinstance(s, dict) and "path" in s}
            print(f"[file_summarizer] Loaded {len(existing)} existing summaries (resume mode)")
        except Exception as exc:
            print(f"[file_summarizer] WARNING: could not load existing summaries: {exc}")

    # Collect files to process
    all_files = _collect_files(_CODEBASE_DIR)
    total = len(all_files)
    print(f"[file_summarizer] Found {total} files to process in {_CODEBASE_DIR}")

    # Determine which files need summarizing
    pending = [f for f in all_files if _relative_path(f, _CODEBASE_DIR) not in existing] if not force_rebuild else all_files
    print(f"[file_summarizer] {len(pending)} files need summarization ({total - len(pending)} cached)")

    # Process in batches
    summaries_map: dict[str, dict] = dict(existing)  # start with cached
    processed = 0
    errors = 0

    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start: batch_start + BATCH_SIZE]

        for path in batch:
            rel = _relative_path(path, _CODEBASE_DIR)
            global_idx = list(all_files).index(path) + 1
            print(f"  [{global_idx}/{total}] {rel}")

            summary = _build_summary(client, model_id, path, _CODEBASE_DIR)
            summaries_map[rel] = summary

            if summary["purpose"].startswith("[LLM error"):
                errors += 1
            processed += 1

            # Checkpoint: save after every file so progress is not lost
            _save_summaries(list(summaries_map.values()))

        if batch_start + BATCH_SIZE < len(pending):
            time.sleep(BATCH_SLEEP_SECONDS)

    _save_summaries(list(summaries_map.values()))
    print(f"[file_summarizer] Done. {processed} processed, {errors} errors. Output: {_OUTPUT_FILE}")
    return _OUTPUT_FILE


def _save_summaries(summaries: list[dict]) -> None:
    """Write summaries list to the output JSON file atomically."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, indent=2, ensure_ascii=False)
    tmp.replace(_OUTPUT_FILE)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build IDRE file summaries")
    parser.add_argument("--force", action="store_true", help="Re-summarize all files")
    args = parser.parse_args()
    build_file_summaries(force_rebuild=args.force)
