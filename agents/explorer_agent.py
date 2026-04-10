# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/explorer_agent.py
=========================
Agentic code-exploration step that reads actual source files from the local
``idre-codebase/`` clone.

Workflow:
1.  Send the ticket + assembled context (file summaries) to the LLM, asking
    it to identify the 3-5 most relevant files and what to look for in each.
2.  For every recommended file: read a representative slice, grep for
    specific terms, and optionally pull recent git commit messages.
3.  Assemble an "exploration report" string that captures exactly what was
    found — this is stored as ``exploration_context`` in WorkflowState and
    feeds directly into the planner prompt.

The LLM call is made through BaseAgent.invoke_llm_structured so it is fully
logged and subject to the standard retry/parse-failure handling.
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agents.base_agent import BaseAgent
from config.settings import settings
from mcp_client.client_factory import get_mcp_client, filter_github_tools
from schemas.workflow_state import WorkflowState
from utils.mcp_helpers import find_tool
from utils.retry import ainvoke_with_retry

# Root of the checked-out codebase (sibling of the hybrid package root)
# NOTE: This is optional - if not present, explorer will be skipped
_CODEBASE_ROOT = Path(__file__).parent.parent / "idre-codebase"
_CODEBASE_AVAILABLE = _CODEBASE_ROOT.exists() and _CODEBASE_ROOT.is_dir()

# ── Pydantic schemas for structured LLM output ───────────────────────────────


class FileToExplore(BaseModel):
    """A single file the LLM wants the explorer to read."""

    file_path: str = Field(
        ...,
        description="Relative path to the file within the codebase, e.g. src/components/banking/BankAccountList.tsx",
    )
    what_to_look_for: str = Field(
        ...,
        description="Short description of the specific logic, symbol, or pattern to find in this file.",
    )


class ExplorationPlan(BaseModel):
    """LLM-generated plan listing which files to explore and why."""

    files: list[FileToExplore] = Field(
        default_factory=list,
        description="Ordered list of files to read, most relevant first (5-8 entries).",
    )
    reasoning: str = Field(
        ...,
        description="1-2 sentences explaining why these files were chosen.",
    )


# ── Low-level file-system / git tools ────────────────────────────────────────


def read_file(path: str, start_line: int = 1, max_lines: int = 80) -> str:
    """
    Read ``max_lines`` lines starting from ``start_line`` (1-indexed) of the
    file at ``idre-codebase/{path}``.

    Returns the content as a string, or an error message prefixed with
    ``[ERROR]`` if the file cannot be read.
    """
    full_path = _CODEBASE_ROOT / path
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()

        start_idx = max(0, start_line - 1)
        slice_lines = all_lines[start_idx : start_idx + max_lines]
        return "".join(slice_lines)
    except FileNotFoundError:
        return f"[ERROR] File not found: {path}"
    except Exception as exc:
        return f"[ERROR] Could not read {path}: {exc}"


def grep_code(
    pattern: str,
    file_glob: str = "**/*.ts*",
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """
    Search for *pattern* (regex) across files matching *file_glob* inside
    ``idre-codebase/``.

    Returns a list of dicts with keys ``path``, ``line_num``, ``line_content``
    (up to *max_results* entries).
    """
    results: list[dict[str, Any]] = []
    if not _CODEBASE_ROOT.exists():
        return results

    try:
        compiled = re.compile(pattern)
    except re.error:
        return results

    for file_path in _CODEBASE_ROOT.glob(file_glob):
        if len(results) >= max_results:
            break
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                for line_num, line in enumerate(fh, start=1):
                    if compiled.search(line):
                        rel = file_path.relative_to(_CODEBASE_ROOT).as_posix()
                        results.append(
                            {
                                "path": rel,
                                "line_num": line_num,
                                "line_content": line.rstrip("\n"),
                            }
                        )
                        if len(results) >= max_results:
                            break
        except Exception:
            continue

    return results


def get_imports(path: str) -> list[str]:
    """
    Extract import statement paths/modules from a file.

    Handles TypeScript/JavaScript ``import ... from '...'`` and Python
    ``import ...`` / ``from ... import ...`` styles.

    Returns a list of imported path/module strings (de-duplicated).
    """
    full_path = _CODEBASE_ROOT / path
    seen: set[str] = set()
    imports: list[str] = []

    _ts_import_re = re.compile(r"""from\s+['"]([^'"]+)['"]""")
    _ts_require_re = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
    _py_import_re = re.compile(
        r"""^(?:from\s+(\S+)\s+import|import\s+(\S+))""", re.MULTILINE
    )

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return imports

    suffix = Path(path).suffix.lower()
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        for match in _ts_import_re.finditer(content):
            mod = match.group(1)
            if mod not in seen:
                seen.add(mod)
                imports.append(mod)
        for match in _ts_require_re.finditer(content):
            mod = match.group(1)
            if mod not in seen:
                seen.add(mod)
                imports.append(mod)
    elif suffix == ".py":
        for match in _py_import_re.finditer(content):
            mod = match.group(1) or match.group(2)
            if mod and mod not in seen:
                seen.add(mod)
                imports.append(mod)

    return imports


def get_exports(path: str) -> list[str]:
    """
    Extract export names from a TypeScript/JavaScript file.

    Returns a list of exported symbol names (de-duplicated).
    """
    full_path = _CODEBASE_ROOT / path
    seen: set[str] = set()
    exports: list[str] = []

    _export_re = re.compile(
        r"""export\s+(?:default\s+)?(?:(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+)?(\w+)"""
    )
    _reexport_re = re.compile(r"""export\s*\{([^}]+)\}""")

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return exports

    for match in _export_re.finditer(content):
        name = match.group(1)
        if name and name not in seen:
            seen.add(name)
            exports.append(name)

    for match in _reexport_re.finditer(content):
        for name in match.group(1).split(","):
            name = name.strip().split(" as ")[0].strip()
            if name and name not in seen:
                seen.add(name)
                exports.append(name)

    return exports


def find_references(symbol: str, file_glob: str = "**/*.ts*", max_results: int = 15) -> list[dict[str, Any]]:
    """
    Find all usages of *symbol* (function name, class, type, etc.) across
    the codebase.  Returns a list of dicts with ``path``, ``line_num``,
    ``line_content``.
    """
    return grep_code(
        pattern=rf"\b{re.escape(symbol)}\b",
        file_glob=file_glob,
        max_results=max_results,
    )


def get_importers(path: str) -> list[dict[str, Any]]:
    """
    Find all files that import from *path*.

    Searches for ``from '...<path_stem>'`` or ``require('...<path_stem>')``
    patterns across the codebase. Returns grep-style results.
    """
    # Build a pattern that matches the relative import to this file
    stem = Path(path).stem                    # e.g. "BankAccountList"
    parent = Path(path).parent.as_posix()     # e.g. "components/banking"

    # Match imports that end with this file's stem (with or without extension)
    pattern = rf"""from\s+['\"].*{re.escape(stem)}['\"]"""
    results = grep_code(pattern=pattern, file_glob="**/*.ts*", max_results=20)

    # Filter out self-references
    return [r for r in results if r["path"] != path]


def get_prisma_model(model_name: str) -> str:
    """
    Return the full Prisma model definition for *model_name* from
    ``prisma/schema.prisma``.

    Returns the model block text, or an error message if not found.
    """
    schema_path = _CODEBASE_ROOT / "prisma" / "schema.prisma"
    if not schema_path.exists():
        return f"[ERROR] prisma/schema.prisma not found"

    try:
        content = schema_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[ERROR] Could not read schema.prisma: {exc}"

    # Find "model <name> {" and capture until the closing "}"
    pattern = re.compile(
        rf"^model\s+{re.escape(model_name)}\s*\{{.*?^}}",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if match:
        return match.group(0)
    return f"[NOT FOUND] Model '{model_name}' not in prisma/schema.prisma"


def get_api_route(route_path: str) -> str:
    """
    Read the API route handler file for the given route path.

    *route_path* should be like ``/api/banking/accounts`` or
    ``api/banking/accounts``.  Resolves to ``app/api/banking/accounts/route.ts``.
    """
    # Normalise the route path
    clean = route_path.strip("/")
    if not clean.startswith("api/"):
        clean = f"api/{clean}"

    # Next.js App Router convention: app/<route>/route.ts
    candidates = [
        f"app/{clean}/route.ts",
        f"app/{clean}/route.tsx",
        f"app/({clean.split('/')[0]})/{'/'.join(clean.split('/')[1:])}/route.ts",  # grouped routes
    ]

    for candidate in candidates:
        full = _CODEBASE_ROOT / candidate
        if full.exists():
            return read_file(candidate, start_line=1, max_lines=120)

    # Try a broader search
    hits = grep_code(
        pattern=re.escape(clean.split("/")[-1]),
        file_glob="**/route.ts*",
        max_results=5,
    )
    if hits:
        best = hits[0]["path"]
        return f"[Nearest match: {best}]\n" + read_file(best, start_line=1, max_lines=120)

    return f"[NOT FOUND] No route handler for {route_path}"


def similar_past_prs(query: str) -> str:
    """
    Search git log for commits with messages similar to *query*.

    Since idre-codebase may be a snapshot without full git history,
    this falls back to searching code comments and recent changes.
    """
    # Try git log search first
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--all", f"--grep={query}", "-n", "10"],
            cwd=str(_CODEBASE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"Git commits matching '{query}':\n{result.stdout}"
    except Exception:
        pass

    # Fallback: search for TODO/FIXME/ticket references in code
    hits = grep_code(
        pattern=re.escape(query),
        file_glob="**/*.ts*",
        max_results=10,
    )
    if hits:
        lines = "\n".join(f"  {h['path']}:{h['line_num']}: {h['line_content']}" for h in hits)
        return f"Code references matching '{query}':\n{lines}"

    return f"(no past PRs or code references found for '{query}')"


def git_recent_changes(path: str, n: int = 5) -> list[str]:
    """
    Return the last *n* git commit messages for ``idre-codebase/{path}``.

    Returns an empty list if git is unavailable or the file has no history.
    """
    full_path = _CODEBASE_ROOT / path
    if not full_path.exists():
        return []

    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--pretty=format:%s", "--", str(full_path)],
            cwd=str(_CODEBASE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


# ── GitHub fetching (when local codebase not available) ──────────────────────


async def _fetch_file_from_github(tools: list, file_path: str) -> str:
    """
    Fetch file content from GitHub via MCP.

    Returns file content (up to 3000 chars) or error message.
    """
    get_contents = find_tool(tools, "get_file_contents") or find_tool(tools, "contents")
    if get_contents is None:
        return "[ERROR] GitHub get_file_contents tool not found"

    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    try:
        result = await ainvoke_with_retry(
            get_contents,
            {"owner": owner, "repo": repo, "path": file_path}
        )

        if isinstance(result, dict):
            content = result.get("content", "")
            encoding = result.get("encoding", "")

            if encoding == "base64":
                try:
                    content = base64.b64decode(content).decode("utf-8", errors="replace")
                except Exception:
                    return "[ERROR] Could not decode file content"

            return content[:5000]  # Limit to 5000 chars

        return str(result)[:3000]

    except Exception as exc:
        return f"[ERROR] Could not fetch {file_path}: {exc}"


async def _github_grep_code(tools: list, pattern: str, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search for pattern in repository using GitHub code search.

    Returns list of dicts with 'path', 'line_num', 'line_content'.
    """
    search_tool = find_tool(tools, "search_code") or find_tool(tools, "search")
    if search_tool is None:
        return []

    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    try:
        result = await ainvoke_with_retry(
            search_tool,
            {"q": f"{pattern} repo:{owner}/{repo}"}
        )

        items = result.get("items", []) if isinstance(result, dict) else []
        results = []

        for item in items[:max_results]:
            results.append({
                "path": item.get("path", ""),
                "line_num": 0,  # GitHub search doesn't provide line numbers
                "line_content": item.get("text_matches", [{}])[0].get("fragment", "") if item.get("text_matches") else "",
            })

        return results

    except Exception:
        return []


# ── Report builder ────────────────────────────────────────────────────────────


async def _build_file_report_github(file_entry: FileToExplore, tools: list) -> str:
    """
    Read a file from GitHub and produce a formatted exploration report block.

    Similar to _build_file_report but fetches from GitHub instead of local disk.
    """
    path = file_entry.file_path
    what = file_entry.what_to_look_for

    header = f"=== FILE: {path} (from GitHub) ===\nLooking for: {what}\n"

    # --- file content ---
    content = await _fetch_file_from_github(tools, path)
    if content.startswith("[ERROR]"):
        return header + content + "\n"

    lines = content.splitlines()
    numbered = "\n".join(
        f"  {i + 1:>4}: {line}" for i, line in enumerate(lines[:80])  # Limit to 80 lines
    )
    content_block = f"\n[Lines 1-{min(len(lines), 80)}]\n{numbered}\n"

    # --- grep for key terms (using GitHub search) ---
    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", what)
    grep_blocks: list[str] = []
    seen_terms: set[str] = set()

    for term in raw_terms[:3]:  # grep at most 3 terms
        if term.lower() in seen_terms:
            continue
        seen_terms.add(term.lower())

        hits = await _github_grep_code(tools, term, max_results=5)
        if hits:
            hit_lines = "\n".join(
                f"  {h['path']}: {h['line_content'][:100]}"
                for h in hits if h['path'] == path
            )
            if hit_lines:
                grep_blocks.append(f"\ngrep('{term}'): matches in file\n{hit_lines}")

    grep_section = "".join(grep_blocks) if grep_blocks else "\n(no grep results)"

    # --- Extract imports/exports from content ---
    imports = []
    exports_list = []

    suffix = Path(path).suffix.lower()
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        # Extract imports
        import_matches = re.findall(r"""from\s+['"]([^'"]+)['"]""", content)
        imports = import_matches[:10]

        # Extract exports
        export_matches = re.findall(
            r"""export\s+(?:default\s+)?(?:(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+)?(\w+)""",
            content
        )
        exports_list = list(set(export_matches))[:10]

    imports_section = "\nImports: " + ", ".join(imports) if imports else ""
    exports_section = "\nExports: " + ", ".join(exports_list) if exports_list else ""

    # Note: Recent changes from GitHub would require commits API, skipping for now
    history_section = "\nRecent changes: (GitHub mode - history not fetched)"

    return header + content_block + grep_section + imports_section + exports_section + history_section + "\n"


def _build_file_report(file_entry: FileToExplore) -> str:
    """
    Read a file and produce a formatted exploration report block.

    The block includes:
    - The first 80 lines of the file (or the most relevant slice)
    - grep results for key terms extracted from what_to_look_for
    - Recent git commit messages
    """
    path = file_entry.file_path
    what = file_entry.what_to_look_for

    header = f"=== FILE: {path} ===\nLooking for: {what}\n"

    # --- file content ---
    content = read_file(path, start_line=1, max_lines=150)
    if content.startswith("[ERROR]"):
        return header + content + "\n"

    lines = content.splitlines()
    numbered = "\n".join(
        f"  {i + 1:>4}: {line}" for i, line in enumerate(lines)
    )
    content_block = f"\n[Lines 1-{len(lines)}]\n{numbered}\n"

    # --- grep for key terms extracted from what_to_look_for ---
    # Extract candidate identifiers: camelCase words, quoted strings, etc.
    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", what)
    grep_blocks: list[str] = []
    seen_terms: set[str] = set()

    for term in raw_terms[:3]:  # grep at most 3 terms to keep report concise
        if term.lower() in seen_terms:
            continue
        seen_terms.add(term.lower())

        hits = grep_code(
            pattern=re.escape(term),
            file_glob=f"**/{Path(path).name}",
            max_results=5,
        )
        if hits:
            hit_lines = "\n".join(
                f"  Line {h['line_num']}: {h['line_content']}"
                for h in hits
            )
            grep_blocks.append(f"\ngrep('{term}'): {len(hits)} match(es)\n{hit_lines}")

    grep_section = "".join(grep_blocks) if grep_blocks else "\n(no grep results)"

    # --- imports from this file ---
    imports = get_imports(path)
    if imports:
        imports_section = "\nImports: " + ", ".join(imports[:10])
    else:
        imports_section = ""

    # --- who imports this file ---
    importers = get_importers(path)
    if importers:
        importer_paths = list({r["path"] for r in importers})[:5]
        importers_section = "\nImported by: " + ", ".join(importer_paths)
    else:
        importers_section = ""

    # --- exports from this file ---
    file_exports = get_exports(path)
    if file_exports:
        exports_section = "\nExports: " + ", ".join(file_exports[:10])
    else:
        exports_section = ""

    # --- recent git history ---
    recent = git_recent_changes(path, n=3)
    if recent:
        history_section = "\nRecent changes:\n" + "\n".join(f'  "{msg}"' for msg in recent)
    else:
        history_section = "\nRecent changes: (no git history or git unavailable)"

    return header + content_block + grep_section + imports_section + importers_section + exports_section + history_section + "\n"


# ── Agent ────────────────────────────────────────────────────────────────────

_EXPLORER_SYSTEM = (
    "You are a senior software engineer performing targeted code exploration "
    "to understand how an existing codebase implements the features touched by "
    "a JIRA ticket.  Your goal is to identify the most relevant source files "
    "and describe precisely what to look for in each — so that a planner agent "
    "can write an accurate, non-hallucinated implementation plan.\n\n"
    "Return only the structured JSON that matches the ExplorationPlan schema.  "
    "Prefer specificity: name actual component files, hooks, services, or API "
    "routes rather than generic folders.\n\n"
    "Adjust your exploration scope based on ticket type:\n"
    "- bug_fix: focus on 3-5 files — the function that is broken, its direct callers, "
    "downstream consumers that may need updates, and any related test files.\n"
    "- feature: explore 5-8 files across the full flow — entry point (route/action), "
    "service/business logic, data layer, affected UI components, and permission/auth files if relevant.\n"
    "- ui_change: prioritise component files, their parent containers, hooks, and "
    "style/config files. 3-5 files.\n"
    "- refactor: identify all call sites and dependents of the symbol being changed. "
    "5-8 files minimum.\n"
    "- other: default to 4-6 files covering the most likely change surface.\n\n"
    "IMPORTANT: For permission/role changes, trace ALL downstream files that check "
    "those permissions (route configs, client utils, sidebar, dashboards). "
    "For email/payment changes, identify the correct architectural layer — "
    "check whether the change belongs in the payment service, email service, "
    "webhook handler, or UI component by reading the actual call chain."
)

_EXPLORER_HUMAN_TEMPLATE = """\
## JIRA Ticket
**ID**: {ticket_id}
**Title**: {title}
**Type**: {ticket_type}

**Description**:
{description}

**Acceptance Criteria**:
{acceptance_criteria}

**Attachments**: {attachments}

---

## Knowledge Base Context
Primary module: {module_name}

### File Summaries (most relevant files)
{file_summaries}

### Repo Map (module overview)
{repo_map_section}

### Co-Change Patterns (files that historically change together in git)
{co_change_hints}

---

## Task
Identify the source files that a developer would need to read and modify to
implement this ticket. Use the scope guidance from the system prompt for how
many files to select based on ticket type **{ticket_type}**.

For each file, describe the specific function, component, hook, class, or
logic block to look for.

Focus on files that are likely to require changes — not every file that
touches this area.  Be concrete: name specific symbol names if you know them
from the summaries above.
"""


class ExplorerAgent(BaseAgent):
    """
    Agentic explorer that reads actual codebase files to build precise
    context for the planner.
    """

    def run(self, state: WorkflowState) -> dict:
        ticket_context = state.get("ticket_context")
        ticket_type = state.get("ticket_type") or "other"
        assembled_context = state.get("assembled_context") or {}
        run_id = state.get("run_id", "unknown")
        ticket_id = state.get("ticket_id", "unknown")

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase="code_exploration",
        )

        # If local codebase not available, use GitHub MCP instead
        use_github = not _CODEBASE_AVAILABLE
        if use_github:
            self.logger.info(
                "explorer_using_github",
                ticket_id=ticket_id,
                run_id=run_id,
                message="Local codebase not available - explorer will fetch from GitHub via MCP",
            )

        if ticket_context is None:
            self.logger.warning(
                "explorer_no_ticket_context",
                ticket_id=ticket_id,
                run_id=run_id,
            )
            return {
                "exploration_context": "(no ticket context — exploration skipped)",
                "llm_call_ids": [],
            }

        title = getattr(ticket_context, "title", "") or ""
        description = getattr(ticket_context, "description", "") or "(empty)"
        ac = getattr(ticket_context, "acceptance_criteria", "") or "(not provided)"

        module_name = assembled_context.get("module_name", "general")
        file_summaries = assembled_context.get("file_summaries_section", "(unavailable)")
        repo_map_section = assembled_context.get("repo_map_section", "(unavailable)")

        # ── Step 1: Ask LLM which files to explore ──────────────────────────
        co_change_hints = assembled_context.get("co_change_hints", "(none)")

        # Build attachment summary
        att_list = getattr(ticket_context, "attachments", []) or []
        if att_list:
            att_text = ", ".join(
                f"{a.filename} ({a.content_type})" for a in att_list
            )
        else:
            att_text = "(none)"

        human_prompt = _EXPLORER_HUMAN_TEMPLATE.format(
            ticket_id=ticket_id,
            title=title,
            ticket_type=ticket_type,
            description=description,
            acceptance_criteria=ac,
            attachments=att_text,
            module_name=module_name,
            file_summaries=file_summaries[:5000],
            repo_map_section=repo_map_section[:3000],
            co_change_hints=co_change_hints[:2000],
        )

        try:
            plan, call_id = self.invoke_llm_structured(
                system_prompt=_EXPLORER_SYSTEM,
                human_prompt=human_prompt,
                output_schema=ExplorationPlan,
                run_id=run_id,
                ticket_id=ticket_id,
                prompt_template_name="explorer_planning",
            )
        except Exception as exc:
            self.logger.error(
                "explorer_llm_failed",
                exc=exc,
                ticket_id=ticket_id,
                run_id=run_id,
            )
            return {
                "exploration_context": f"(explorer LLM call failed: {exc})",
                "llm_call_ids": [],
            }

        if plan is None:
            self.logger.warning(
                "explorer_plan_none",
                ticket_id=ticket_id,
                run_id=run_id,
            )
            return {
                "exploration_context": "(explorer could not produce an exploration plan)",
                "llm_call_ids": [call_id] if call_id else [],
            }

        self.logger.info(
            "explorer_plan_received",
            ticket_id=ticket_id,
            run_id=run_id,
            files_to_explore=[f.file_path for f in plan.files],
            reasoning=plan.reasoning,
        )

        # ── Step 2: Read each file and build the exploration report ─────────
        report_parts: list[str] = [
            f"# Code Exploration Report\n"
            f"Ticket: {ticket_id} — {title}\n"
            f"Explorer reasoning: {plan.reasoning}\n"
        ]

        # Track Prisma models and API routes mentioned across all files
        prisma_models_seen: set[str] = set()
        api_routes_seen: set[str] = set()

        if use_github:
            # Fetch files from GitHub via MCP
            async def explore_files_github():
                """Helper to fetch files from GitHub asynchronously"""
                async with get_mcp_client() as client:
                    all_tools = await client.get_tools()
                    gh_tools = filter_github_tools(all_tools)

                    skipped: list[str] = []
                    for file_entry in plan.files[:8]:  # Safety cap at 8 files
                        try:
                            block = await _build_file_report_github(file_entry, gh_tools)

                            # Skip files that could not be fetched — don't pollute
                            # the report with [ERROR] blocks that confuse the planner
                            first_content_line = block.split("\n", 3)[2] if block.count("\n") >= 2 else block
                            if "[ERROR]" in first_content_line:
                                skipped.append(file_entry.file_path)
                                self.logger.warning(
                                    "explorer_file_skipped",
                                    path=file_entry.file_path,
                                    reason="fetch_error",
                                )
                                continue

                            report_parts.append(block)

                            # Detect Prisma model references
                            for word in re.findall(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", file_entry.what_to_look_for):
                                prisma_models_seen.add(word)

                            # Detect API route references
                            route_matches = re.findall(r"/api/[\w/\-]+", file_entry.what_to_look_for)
                            api_routes_seen.update(route_matches)

                        except Exception as exc:
                            skipped.append(file_entry.file_path)
                            self.logger.warning(
                                "explorer_file_exception",
                                path=file_entry.file_path,
                                error=str(exc),
                            )

                    if skipped:
                        report_parts.append(
                            f"\n[Note: {len(skipped)} file(s) could not be fetched and were skipped: "
                            f"{', '.join(skipped)}]"
                        )

            # Run the async exploration
            self.run_async(explore_files_github())

        else:
            # Read from local codebase
            skipped_local: list[str] = []
            for file_entry in plan.files[:8]:  # Safety cap at 8 files
                try:
                    block = _build_file_report(file_entry)

                    # Skip files that returned an error
                    if block.startswith(f"=== FILE: {file_entry.file_path} ===\n[ERROR]"):
                        skipped_local.append(file_entry.file_path)
                        self.logger.warning(
                            "explorer_file_skipped",
                            path=file_entry.file_path,
                            reason="read_error",
                        )
                        continue

                    report_parts.append(block)

                    # Detect Prisma model references in what_to_look_for or file content
                    for word in re.findall(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", file_entry.what_to_look_for):
                        prisma_models_seen.add(word)

                    # Detect API route references
                    route_matches = re.findall(r"/api/[\w/\-]+", file_entry.what_to_look_for)
                    api_routes_seen.update(route_matches)

                except Exception as exc:
                    skipped_local.append(file_entry.file_path)
                    self.logger.warning(
                        "explorer_file_exception",
                        path=file_entry.file_path,
                        error=str(exc),
                    )

            if skipped_local:
                report_parts.append(
                    f"\n[Note: {len(skipped_local)} file(s) could not be read and were skipped: "
                    f"{', '.join(skipped_local)}]"
                )

            # Auto-explore Prisma models that were referenced (local mode only)
            for model in list(prisma_models_seen)[:3]:
                model_def = get_prisma_model(model)
                if not model_def.startswith("[NOT FOUND]") and not model_def.startswith("[ERROR]"):
                    report_parts.append(f"\n=== PRISMA MODEL: {model} ===\n{model_def}\n")

            # Auto-explore API routes that were referenced (local mode only)
            for route in list(api_routes_seen)[:2]:
                route_code = get_api_route(route)
                if not route_code.startswith("[NOT FOUND]"):
                    report_parts.append(f"\n=== API ROUTE: {route} ===\n{route_code}\n")

        exploration_report = "\n".join(report_parts)

        self.logger.info(
            "agent_node_completed",
            ticket_id=ticket_id,
            run_id=run_id,
            files_explored=len(plan.files),
            report_chars=len(exploration_report),
        )

        return {
            "exploration_context": exploration_report,
            "llm_call_ids": [call_id] if call_id else [],
            "total_llm_calls": state.get("total_llm_calls", 0) + 1,
        }


_agent = ExplorerAgent()


def explorer_node(state: WorkflowState) -> dict:
    """LangGraph node entry point for the explorer agent."""
    return _agent.run(state)
