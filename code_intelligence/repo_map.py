# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
"""
repo_map.py — Generate a compact, human-readable repo map from the IDRE codebase.

No LLM required. Walks the file tree, extracts top-level export/model signatures
with regex, and writes a grouped, sorted text file to data/repo_map.txt.
"""

import re
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_HYBRID_DIR = _THIS_DIR.parent
_CODEBASE_DIR = _HYBRID_DIR / "idre-codebase"
_DATA_DIR = _THIS_DIR / "data"
_OUTPUT_FILE = _DATA_DIR / "repo_map.txt"

# ── Configuration ────────────────────────────────────────────────────────────

SOURCE_EXTENSIONS = {".ts", ".tsx", ".prisma", ".sql", ".js", ".mjs"}
SKIP_DIRS = {"node_modules", ".next", ".git", "__pycache__", "dist", ".turbo"}

# Approximate token budget: ~25K tokens ≈ 100K chars (expanded for SQL + test files)
MAX_OUTPUT_CHARS = 100_000

# ── Export extraction (TypeScript/TSX) ───────────────────────────────────────

# Ordered list of (pattern, label) pairs; first match wins per line
_TS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # export default function Foo
    (re.compile(r"^export\s+default\s+function\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "function"),
    # export default class Foo
    (re.compile(r"^export\s+default\s+class\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "class"),
    # export async function foo / export function foo
    (re.compile(r"^export\s+(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "function"),
    # export class Foo
    (re.compile(r"^export\s+class\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "class"),
    # export interface Foo
    (re.compile(r"^export\s+interface\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "interface"),
    # export type Foo =
    (re.compile(r"^export\s+type\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[=<{]"), "type"),
    # export const foo = / export const foo: ...
    (re.compile(r"^export\s+const\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "const"),
    # export enum Foo
    (re.compile(r"^export\s+enum\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "enum"),
    # export { Foo, Bar } — named re-exports
    (re.compile(r"^export\s+\{([^}]+)\}"), "re-export"),
    # export default (anonymous arrow / object)
    (re.compile(r"^export\s+default\s+"), "default"),
]

# ── Prisma model extraction ───────────────────────────────────────────────────

_PRISMA_MODEL_PATTERN = re.compile(r"^model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_PRISMA_ENUM_PATTERN = re.compile(r"^enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")


def _extract_ts_exports(content: str) -> list[str]:
    """Return a flat list of export names extracted from TypeScript/TSX source."""
    exports: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("export"):
            continue
        matched = False
        for pattern, label in _TS_PATTERNS:
            m = pattern.match(line)
            if m:
                if label == "re-export":
                    # Extract individual names from { Foo, Bar as Baz }
                    raw = m.group(1)
                    names = [
                        part.split(" as ")[-1].strip()
                        for part in raw.split(",")
                        if part.strip() and not part.strip().startswith("//")
                    ]
                    exports.extend(n for n in names if n and re.match(r"^[A-Za-z_$]", n))
                elif label == "default":
                    exports.append("default")
                else:
                    exports.append(m.group(1))
                matched = True
                break
        # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for e in exports:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped


_SQL_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]*(\w+)[`\"]*", re.IGNORECASE)
_SQL_ALTER_TABLE = re.compile(r"ALTER\s+TABLE\s+[`\"]*(\w+)[`\"]*", re.IGNORECASE)
_SQL_ADD_COLUMN = re.compile(r"ADD\s+(?:COLUMN\s+)?[`\"]*(\w+)[`\"]*", re.IGNORECASE)


def _extract_sql_symbols(content: str) -> list[str]:
    """Return table and column names from SQL migration files."""
    symbols: list[str] = []
    seen: set[str] = set()
    for m in _SQL_CREATE_TABLE.finditer(content):
        name = f"table:{m.group(1)}"
        if name not in seen:
            seen.add(name)
            symbols.append(name)
    for m in _SQL_ALTER_TABLE.finditer(content):
        name = f"alter:{m.group(1)}"
        if name not in seen:
            seen.add(name)
            symbols.append(name)
    return symbols[:10]  # cap to avoid bloat


def _extract_prisma_symbols(content: str) -> list[str]:
    """Return model and enum names from a .prisma file."""
    symbols: list[str] = []
    for line in content.splitlines():
        m = _PRISMA_MODEL_PATTERN.match(line.strip())
        if m:
            symbols.append(f"model:{m.group(1)}")
            continue
        m = _PRISMA_ENUM_PATTERN.match(line.strip())
        if m:
            symbols.append(f"enum:{m.group(1)}")
    return symbols


# ── File collection ───────────────────────────────────────────────────────────

def _is_skip_dir(path: Path, base: Path) -> bool:
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    return any(part in SKIP_DIRS for part in rel.parts)


def _should_process(path: Path) -> bool:
    return path.suffix in SOURCE_EXTENSIONS


def _collect_files(codebase_dir: Path) -> list[Path]:
    results: list[Path] = []
    for path in sorted(codebase_dir.rglob("*")):
        if not path.is_file():
            continue
        if _is_skip_dir(path, codebase_dir):
            continue
        if _should_process(path):
            results.append(path)
    return results


# ── Map builder ───────────────────────────────────────────────────────────────

def _build_file_entry(path: Path, codebase_dir: Path) -> tuple[str, str]:
    """
    Return (relative_posix_path, symbols_string) for one file.
    symbols_string is comma-joined export/model names, or empty string.
    """
    rel = path.relative_to(codebase_dir).as_posix()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return rel, ""

    if path.suffix == ".prisma":
        symbols = _extract_prisma_symbols(content)
    elif path.suffix == ".sql":
        symbols = _extract_sql_symbols(content)
    else:
        symbols = _extract_ts_exports(content)

    return rel, ", ".join(symbols)


def build_repo_map(codebase_dir: Path | None = None) -> str:
    """
    Generate the compact repo map text and save to data/repo_map.txt.

    Returns the full text content of the map.
    """
    if codebase_dir is None:
        codebase_dir = _CODEBASE_DIR

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_files = _collect_files(codebase_dir)
    total = len(all_files)
    print(f"[repo_map] Processing {total} files in {codebase_dir}")

    # Group by directory (relative POSIX)
    dir_groups: dict[str, list[tuple[str, str]]] = {}
    for path in all_files:
        rel, symbols = _build_file_entry(path, codebase_dir)
        dir_key = str(Path(rel).parent.as_posix()) if Path(rel).parent != Path(".") else "."
        dir_groups.setdefault(dir_key, []).append((rel, symbols))

    # Build output lines grouped by directory
    lines: list[str] = []
    lines.append(f"# IDRE Codebase Repo Map — {total} files")
    lines.append("# Format: path — exports/symbols")
    lines.append("")

    chars_written = sum(len(l) + 1 for l in lines)

    for dir_key in sorted(dir_groups.keys()):
        entries = dir_groups[dir_key]

        header = f"\n## {dir_key}/"
        lines.append(header)
        chars_written += len(header) + 1

        for rel, symbols in sorted(entries):
            if symbols:
                entry = f"  {rel} — {symbols}"
            else:
                entry = f"  {rel}"
            lines.append(entry)
            chars_written += len(entry) + 1

            if chars_written >= MAX_OUTPUT_CHARS:
                lines.append("")
                lines.append(f"# [truncated — {MAX_OUTPUT_CHARS} char budget reached]")
                break

        if chars_written >= MAX_OUTPUT_CHARS:
            break

    text = "\n".join(lines)
    _OUTPUT_FILE.write_text(text, encoding="utf-8")
    print(f"[repo_map] Written to {_OUTPUT_FILE} ({len(text):,} chars)")
    return text


if __name__ == "__main__":
    build_repo_map()
