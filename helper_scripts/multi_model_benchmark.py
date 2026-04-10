"""
IDRE Artoo — Multi-Model Benchmark
=====================================
Runs the hybrid Artoo pipeline for the latest 40 completed IDRE tickets
across 3 best LLM models and compares results.

READ-ONLY: Zero writes to JIRA or Confluence.

Models tested:
  1. gemini-2.5-pro (Google)
  2. gpt-5.2 (OpenAI)
  3. claude-sonnet-4-6 (AWS Bedrock) — fallback if others rate-limited

Features:
  - Circuit breaker: stops on first rate-limit error (prevents quota burn)
  - Resume capability: skips already-completed tickets on restart
  - Per-model rate limiting (Gemini 4s, OpenAI 1s, Bedrock 1.2s)
  - Parallel ticket processing within each model (max 5 concurrent)

Parallelism:
  - JIRA + Confluence fetching: parallel (threads)
  - Ticket runs within each model: parallel (ThreadPoolExecutor, max 5)
  - Model runs: sequential (avoids settings race-condition)
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

# ── stdout fix for Windows (guard against double-wrapping) ───────────────────
if hasattr(sys.stdout, "buffer") and not isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and not isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
load_dotenv(HERE / ".env", override=True)

from config.settings import settings
from schemas.ticket import TicketContext
from schemas.confluence import ConfluenceContext, ConfluencePage
from schemas.workflow_state import WorkflowPhase
from agents.completeness_agent import CompletenessAgent
from agents.planner_agent import PlannerAgent
from agents.code_proposal_agent import CodeProposalAgent
from agents.test_agent import TestAgent
from agents.file_validator_agent import file_validator_node
from utils.retry import circuit_breaker, RateLimitBreaker

# ── Output directories ────────────────────────────────────────────────────────
RESULTS_ROOT = HERE / "multi_model_results"
JIRA_CACHE   = RESULTS_ROOT / "jiras"
CONF_CACHE   = RESULTS_ROOT / "confluence_cache"
RESULTS_ROOT.mkdir(exist_ok=True)
JIRA_CACHE.mkdir(exist_ok=True)
CONF_CACHE.mkdir(exist_ok=True)

# ── JIRA credentials (read-only) ──────────────────────────────────────────────
JIRA_BASE    = settings.jira_url.rstrip("/")
CONF_BASE    = getattr(settings, "confluence_url", JIRA_BASE + "/wiki")
JIRA_AUTH    = HTTPBasicAuth(settings.jira_username, settings.jira_api_token.get_secret_value())
CONF_SPACE   = "SD"

# ── KT document path ─────────────────────────────────────────────────────────
KT_SUMMARY_PATH = Path(
    r"C:\Users\anand\Downloads\idre-kt-20260406T044545Z-3-001\idre-kt\idre_kt_summary.md"
)

# ── Model configurations ──────────────────────────────────────────────────────
# Best models from each provider (Gemini, OpenAI, Bedrock Claude)
MODELS = [
    {
        "slug":       "gemini-2.5-pro",
        "provider":   "gemini",
        "model_id":   "gemini-2.5-pro",
        "so_method":  "function_calling",
        "max_tokens": 8192,
        "temperature": 0.1,
    },
    {
        "slug":       "gpt-5.2",
        "provider":   "openai",
        "model_id":   "gpt-5.2",
        "so_method":  "json_schema",
        "max_tokens": 8192,
        "temperature": 0.1,
    },
    {
        "slug":       "claude-sonnet-4-6",
        "provider":   "bedrock",
        "model_id":   "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "so_method":  "function_calling",
        "max_tokens": 8192,
        "temperature": 0.1,
    },
]

# ── Target JIRAs ─────────────────────────────────────────────────────────────
# Add 5-10 completed JIRA tickets from your project for benchmarking.
# Pick a diverse set: UI bugs, features, logic bugs, support requests.
TARGET_JIRA_IDS = [
    # "PROJ-100",  # Example: UI bug
    # "PROJ-200",  # Example: Feature request
    # "PROJ-300",  # Example: Logic bug
]

# Model-specific parallelism to avoid rate limits
MAX_PARALLEL_BY_PROVIDER = {
    "bedrock": 2,   # Bedrock: 2 tickets at a time (was hitting throttling at 5)
    "gemini": 1,    # Gemini: 1 ticket at a time (4s rate limit)
    "openai": 1,    # OpenAI: 1 ticket at a time (quota issues)
}
PRINT_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
    def handle_data(self, data: str):
        self._parts.append(data)
    def get_text(self) -> str:
        return " ".join(t.strip() for t in self._parts if t.strip())


def strip_html(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.get_text()


def _flatten_adf(node: Any) -> str:
    """Recursively flatten Atlassian Document Format to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts = []
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []):
            parts.append(_flatten_adf(child))
        return " ".join(p for p in parts if p).strip()
    if isinstance(node, list):
        return " ".join(_flatten_adf(i) for i in node)
    return str(node)


def _search_keywords(text: str, max_words: int = 5) -> str:
    """Extract key non-stopword terms for Confluence search."""
    stopwords = {"the","a","an","is","in","on","at","to","for","of","and","or",
                 "not","be","by","as","with","from","this","that","it","are",
                 "was","has","have","will","can","should","user","when","able"}
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    seen, result = set(), []
    for w in words:
        if w not in stopwords and w not in seen:
            seen.add(w)
            result.append(w)
        if len(result) >= max_words:
            break
    return " ".join(result) or text[:30]


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1A — FETCH JIRAs (read-only, parallel)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_jira(ticket_id: str) -> dict:
    """Fetch a single JIRA ticket via REST API. READ-ONLY (GET only)."""
    cache_file = JIRA_CACHE / f"{ticket_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    # Use /issue/{id} endpoint (still works) — /search/jql is for bulk queries
    url = f"{JIRA_BASE}/rest/api/3/issue/{ticket_id}"
    params = {"expand": "renderedFields,names,schema"}
    r = requests.get(url, params=params, auth=JIRA_AUTH, timeout=30)
    r.raise_for_status()
    data = r.json()
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"  [JIRA] Fetched {ticket_id}: {data['fields'].get('summary','')[:60]}")
    return data


def build_ticket_context(raw: dict) -> TicketContext:
    """Convert raw JIRA API response to TicketContext."""
    fields = raw.get("fields", {})
    ticket_id = raw.get("key", "")

    description = fields.get("description", "") or ""
    if isinstance(description, dict):
        description = _flatten_adf(description)

    ac = fields.get("acceptance_criteria") or fields.get("customfield_10016") or ""
    if isinstance(ac, dict):
        ac = _flatten_adf(ac)
    if not ac and description:
        m = re.search(r"#+\s*Acceptance Criteria\s*\n(.*?)(?=\n#+\s|\Z)",
                      description, re.IGNORECASE | re.DOTALL)
        if m:
            ac = m.group(1).strip()

    priority_obj  = fields.get("priority") or {}
    assignee_obj  = fields.get("assignee") or {}
    reporter_obj  = fields.get("reporter") or {}
    components    = [c.get("name","") for c in (fields.get("components") or [])]
    linked        = [
        lk.get("inwardIssue", {}).get("key","") or lk.get("outwardIssue", {}).get("key","")
        for lk in (fields.get("issuelinks") or [])
    ]

    return TicketContext(
        ticket_id=ticket_id,
        title=fields.get("summary", ""),
        description=description[:4000],
        acceptance_criteria=ac[:2000] if ac else None,
        labels=fields.get("labels", []) or [],
        priority=priority_obj.get("name") if isinstance(priority_obj, dict) else None,
        story_points=fields.get("story_points") or fields.get("customfield_10028"),
        reporter=reporter_obj.get("displayName") if isinstance(reporter_obj, dict) else None,
        assignee=assignee_obj.get("displayName") if isinstance(assignee_obj, dict) else None,
        status=str(fields.get("status", {}).get("name", "")),
        components=components,
        linked_issues=[k for k in linked if k],
        raw_jira_data=raw,
    )


def fetch_all_jiras() -> dict[str, dict]:
    """Fetch all 30 JIRAs in parallel. Returns {ticket_id: raw_data}."""
    _log("\n[Phase 1A] Fetching 30 JIRAs in parallel...")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_jira, tid): tid for tid in TARGET_JIRA_IDS}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                results[tid] = future.result()
            except Exception as e:
                _log(f"  [JIRA] ERROR fetching {tid}: {e}")
                results[tid] = {}
    _log(f"  Fetched {len(results)} JIRAs.\n")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1B — FETCH CONFLUENCE (read-only, parallel)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_confluence_for_ticket(ticket_id: str, summary: str, description: str) -> list[dict]:
    """Search Confluence for pages relevant to a ticket. READ-ONLY (GET only)."""
    cache_file = CONF_CACHE / f"{ticket_id}_pages.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    keywords = _search_keywords(f"{summary} {description}")
    # Use two different queries for better coverage
    pages: list[dict] = []
    seen_ids: set[str] = set()

    for query in [keywords, summary[:50]]:
        if not query.strip():
            continue
        cql = f'space="{CONF_SPACE}" AND type=page AND text~"{query}"'
        try:
            r = requests.get(
                f"{CONF_BASE}/rest/api/content/search",
                params={"cql": cql, "limit": 4, "expand": "body.view"},
                auth=JIRA_AUTH,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            for p in r.json().get("results", []):
                pid = p.get("id", "")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                html = p.get("body", {}).get("view", {}).get("value", "")
                text = strip_html(html)[:2000]
                links = p.get("_links", {})
                web_url = (links.get("base", CONF_BASE) + links.get("webui", ""))
                pages.append({
                    "page_id": pid,
                    "title": p.get("title", ""),
                    "url": web_url,
                    "space_key": CONF_SPACE,
                    "content_excerpt": text,
                    "relevance_reason": f"Matched query: {query}",
                })
            if len(pages) >= 5:
                break
        except Exception as e:
            _log(f"  [CONF] WARN for {ticket_id}: {e}")

    cache_file.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    return pages


def build_confluence_context(pages: list[dict], summary: str, kt_excerpt: str) -> ConfluenceContext:
    """Build ConfluenceContext from fetched pages + KT content."""
    conf_pages = [
        ConfluencePage(
            page_id=p["page_id"] or "kt-0",
            title=p["title"],
            url=p["url"],
            space_key=p["space_key"],
            content_excerpt=p["content_excerpt"][:2000],
            relevance_reason=p["relevance_reason"],
        )
        for p in pages
    ]
    return ConfluenceContext(
        pages_found=conf_pages,
        total_pages_searched=len(pages),
        search_queries_used=[summary[:80]],
        summary=kt_excerpt[:4000],  # KT doc relevant sections go into summary field
    )


def fetch_all_confluence(jira_data: dict[str, dict]) -> dict[str, list[dict]]:
    """Fetch Confluence pages for all tickets in parallel."""
    _log("[Phase 1B] Fetching Confluence context for all tickets in parallel...")
    results: dict[str, list[dict]] = {}

    def _fetch(tid):
        raw = jira_data.get(tid, {})
        fields = raw.get("fields", {})
        summary = fields.get("summary", tid)
        desc = fields.get("description", "") or ""
        if isinstance(desc, dict):
            desc = _flatten_adf(desc)
        return tid, fetch_confluence_for_ticket(tid, summary, desc[:500])

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_fetch, tid) for tid in TARGET_JIRA_IDS]
        for future in as_completed(futures):
            try:
                tid, pages = future.result()
                results[tid] = pages
                _log(f"  [CONF] {tid}: {len(pages)} pages")
            except Exception as e:
                _log(f"  [CONF] ERROR: {e}")
    _log("")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# KT DOCUMENT — section-based retrieval
# ═══════════════════════════════════════════════════════════════════════════════

def load_kt_sections() -> list[tuple[str, str]]:
    """Load KT summary and split into (header, content) pairs."""
    if not KT_SUMMARY_PATH.exists():
        _log(f"  WARN: KT summary not found at {KT_SUMMARY_PATH}")
        return []
    text = KT_SUMMARY_PATH.read_text(encoding="utf-8")
    # Split on ## headers
    chunks = re.split(r"\n(#{1,3} .+)\n", text)
    sections: list[tuple[str, str]] = []
    header = "Overview"
    buf: list[str] = []
    for chunk in chunks:
        if re.match(r"^#{1,3} ", chunk):
            if buf:
                sections.append((header, "\n".join(buf).strip()))
            header = chunk.strip()
            buf = []
        else:
            buf.append(chunk)
    if buf:
        sections.append((header, "\n".join(buf).strip()))
    return sections


def get_kt_excerpt(kt_sections: list[tuple[str, str]], query: str, max_chars: int = 3000) -> str:
    """Find the most relevant KT sections for a ticket query."""
    if not kt_sections:
        return ""
    query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))
    scored = []
    for header, content in kt_sections:
        combined = (header + " " + content).lower()
        score = sum(1 for w in query_words if w in combined)
        scored.append((score, header, content))
    scored.sort(reverse=True)
    parts = []
    total = 0
    for _, header, content in scored[:4]:
        snippet = f"### {header}\n{content[:800]}"
        if total + len(snippet) > max_chars:
            break
        parts.append(snippet)
        total += len(snippet)
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM FACTORY — creates fresh instances (no cache)
# ═══════════════════════════════════════════════════════════════════════════════

def create_llm(model_config: dict):
    """Create a fresh LLM instance for the given model config."""
    provider  = model_config["provider"]
    model_id  = model_config["model_id"]
    temp      = model_config["temperature"]
    max_tok   = model_config["max_tokens"]

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_id,
            api_key=settings.openai_api_key.get_secret_value(),
            temperature=temp,
            max_tokens=max_tok,
            streaming=False,
        )
    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=settings.gemini_api_key.get_secret_value(),
            temperature=temp,
            max_output_tokens=max_tok,
        )
    elif provider == "bedrock":
        import boto3
        from botocore.config import Config
        from langchain_aws import ChatBedrock
        session = boto3.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
            region_name=settings.aws_default_region,
        )
        # Increase read timeout for markdown code proposals (can take 60-90s)
        bedrock_config = Config(read_timeout=180, connect_timeout=10, retries={"max_attempts": 2})
        client = session.client("bedrock-runtime", region_name=settings.aws_default_region, config=bedrock_config)
        return ChatBedrock(
            model_id=model_id,
            client=client,
            model_kwargs={
                "temperature": temp,
                "max_tokens": max_tok,
                "anthropic_version": "bedrock-2023-05-31",
            },
            streaming=False,
        )
    raise ValueError(f"Unknown provider: {provider}")


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER — one ticket × one model
# ═══════════════════════════════════════════════════════════════════════════════

def run_ticket_pipeline(
    ticket_id: str,
    raw_jira: dict,
    conf_pages: list[dict],
    model_config: dict,
    llm,
    retriever,
    kt_sections: list[tuple[str, str]],
    out_dir: Path,
) -> dict:
    """Run the 4-stage Artoo pipeline for one ticket with the given LLM."""
    run_id  = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    slug    = model_config["slug"]
    short   = f"[{slug}|{ticket_id}]"

    # Build contexts
    ticket_ctx = build_ticket_context(raw_jira)
    query      = f"{ticket_ctx.title} {(ticket_ctx.description or '')[:300]}"
    kt_excerpt = get_kt_excerpt(kt_sections, query)

    # RAG: codebase retrieval
    repo_ctx = None
    try:
        repo_ctx = retriever.build_repo_context(query, ticket_id)
    except Exception as e:
        _log(f"  {short} WARN repo_ctx: {e}")

    # Confluence context (from cache + KT)
    conf_ctx = build_confluence_context(conf_pages, ticket_ctx.title, kt_excerpt)

    # Workflow state
    state = {
        "run_id":      run_id,
        "ticket_id":   ticket_id,
        "started_at":  started,
        "current_phase": WorkflowPhase.CHECKING_COMPLETENESS,
        "ticket_context": ticket_ctx,
        "repo_context":   repo_ctx,
        "confluence_context": conf_ctx,
        "is_complete_ticket": None,
        "should_stop": False,
        "errors": [],
        "llm_call_ids": [],
        "mcp_tool_calls": [],
        "total_llm_calls": 0,
        "total_tokens_used": 0,
    }

    result = {
        "ticket_id":    ticket_id,
        "title":        ticket_ctx.title,
        "assignee":     ticket_ctx.assignee,
        "status":       ticket_ctx.status,
        "run_id":       run_id,
        "model":        slug,
        "model_id":     model_config["model_id"],
        "provider":     model_config["provider"],
        "started_at":   started,
        "rag_context": {
            "codebase_files": len(repo_ctx.relevant_files) if repo_ctx else 0,
            "confluence_pages": len(conf_ctx.pages_found),
            "kt_excerpt_chars": len(kt_excerpt),
            "top_files": [f.file_path for f in repo_ctx.relevant_files[:5]] if repo_ctx else [],
        },
        "stages": {},
        "errors": [],
    }

    # Create fresh agent instances with injected LLM
    comp_agent = CompletenessAgent(); comp_agent._llm = llm
    plan_agent = PlannerAgent();      plan_agent._llm = llm
    code_agent = CodeProposalAgent(); code_agent._llm = llm
    test_agent = TestAgent();         test_agent._llm = llm

    # Helper to run a pipeline stage — propagates RateLimitBreaker immediately
    def _run_stage(agent, stage_name: str, stage_num: str):
        """Run a pipeline stage. Raises RateLimitBreaker if rate limit hit."""
        circuit_breaker.check()  # Pre-check before calling
        try:
            out = agent.run(state)
            state.update(out)
            return out
        except RateLimitBreaker:
            raise  # Never swallow rate limit errors
        except Exception as e:
            result["errors"].append(f"{stage_name}: {e}")
            result["stages"][stage_name] = {"error": str(e)}
            _log(f"  {short} [{stage_num}] {stage_name} ERROR: {e}")
            return None

    # Stage 1: Completeness
    try:
        _run_stage(comp_agent, "completeness", "1/4")
        cr = state.get("completeness_result")
        result["stages"]["completeness"] = {
            "score":          round(cr.completeness_score, 3) if cr else None,
            "decision":       str(cr.decision) if cr else None,
            "missing_fields": [mf.field_name for mf in cr.missing_fields] if cr else [],
            "is_complete":    state.get("is_complete_ticket"),
        }
        _log(f"  {short} [1/4] completeness={cr.completeness_score:.0%}" if cr else f"  {short} [1/4] completeness=FAILED")
    except RateLimitBreaker:
        raise
    except Exception as e:
        result["errors"].append(f"completeness: {e}")
        result["stages"]["completeness"] = {"error": str(e)}
        _log(f"  {short} [1/4] ERROR: {e}")
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        return result

    # Stage 2: Planning
    try:
        _run_stage(plan_agent, "planning", "2/4")
        plan = state.get("implementation_plan")
        result["stages"]["planning"] = {
            "summary":    plan.summary if plan else None,
            "risk_level": str(plan.risk_level) if plan else None,
            "confidence": plan.confidence_score if plan else None,
            "steps": [
                {
                    "step":           s.step_number,
                    "title":          s.title,
                    "description":    s.description[:300],
                    "affected_files": s.affected_files,
                }
                for s in (plan.implementation_steps if plan else [])
            ],
        }
        n = len(plan.implementation_steps) if plan else 0
        _log(f"  {short} [2/4] planning={n} steps conf={plan.confidence_score if plan else '?'}")
    except RateLimitBreaker:
        raise
    except Exception as e:
        result["errors"].append(f"planner: {e}")
        result["stages"]["planning"] = {"error": str(e)}
        _log(f"  {short} [2/4] planner ERROR: {e}")

    # Stage 3: Code proposal
    try:
        _run_stage(code_agent, "code_proposal", "3/4")
        cp = state.get("code_proposal")
        result["stages"]["code_proposal"] = {
            "overview":    cp.summary if cp else None,
            "confidence":  cp.confidence_score if cp else None,
            "files_changed": [
                {
                    "file":      fc.file_path,
                    "type":      fc.change_type.value if hasattr(fc.change_type, "value") else str(fc.change_type),
                    "rationale": fc.rationale[:200],
                }
                for fc in (cp.file_changes if cp else [])
            ],
        }
        n = len(cp.file_changes) if cp else 0
        _log(f"  {short} [3/4] code_proposal={n} files conf={cp.confidence_score if cp else '?'}")
    except RateLimitBreaker:
        raise
    except Exception as e:
        result["errors"].append(f"code_proposal: {e}")
        result["stages"]["code_proposal"] = {"error": str(e)}
        _log(f"  {short} [3/4] code_proposal ERROR: {e}")

    # Stage 3.5: Validation
    try:
        circuit_breaker.check()
        out = file_validator_node(state)
        state.update(out)
        fv = state.get("file_validation_result")
        result["stages"]["file_validation"] = fv
        if fv and not fv.get("skipped"):
            _log(f"  {short} [3.5/4] validation: {fv.get('surviving_count')} survived, {fv.get('removed_count')} removed, {fv.get('corrected_count')} corrected")
    except RateLimitBreaker:
        raise
    except Exception as e:
        result["errors"].append(f"file_validation: {e}")
        result["stages"]["file_validation"] = {"error": str(e)}
        _log(f"  {short} [3.5/4] file_validation ERROR: {e}")

    # Stage 4: Tests
    try:
        _run_stage(test_agent, "test_suggestions", "4/4")
        ts = state.get("test_suggestions")
        result["stages"]["test_suggestions"] = {
            "coverage_targets": ts.coverage_targets if ts else [],
            "test_cases": [
                {
                    "name":        tc.test_name,
                    "description": tc.description[:200],
                    "type":        tc.test_type,
                }
                for tc in (ts.test_cases if ts else [])
            ],
        }
        n = len(ts.test_cases) if ts else 0
        _log(f"  {short} [4/4] tests={n} cases")
    except RateLimitBreaker:
        raise
    except Exception as e:
        result["errors"].append(f"tests: {e}")
        result["stages"]["test_suggestions"] = {"error": str(e)}
        _log(f"  {short} [4/4] tests ERROR: {e}")

    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL BATCH RUNNER — one model × all 10 tickets (parallel tickets)
# ═══════════════════════════════════════════════════════════════════════════════

def run_model_batch(
    model_config: dict,
    jira_data: dict[str, dict],
    conf_data: dict[str, list[dict]],
    retriever,
    kt_sections: list[tuple[str, str]],
) -> list[dict]:
    """Run all 10 tickets for one model. Tickets run in parallel."""
    slug     = model_config["slug"]
    provider = model_config["provider"]
    model_dir = RESULTS_ROOT / slug
    model_dir.mkdir(exist_ok=True)

    # Set global settings.llm_provider so BaseAgent picks correct so_method
    settings.llm_provider = provider

    # Create ONE shared LLM instance per model (thread-safe for concurrent reads)
    try:
        llm = create_llm(model_config)
    except Exception as e:
        _log(f"\n  [{slug}] FATAL: Could not create LLM: {e}")
        return []

    _log(f"\n{'='*70}")
    _log(f"  MODEL: {slug}  ({model_config['model_id']})")
    _log(f"{'='*70}")

    all_results: list[dict] = []
    results_lock = threading.Lock()

    rate_limit_hit = threading.Event()  # Signals all threads to stop

    def _run_one(tid: str) -> Optional[dict]:
        # Check if another thread already hit a rate limit
        if rate_limit_hit.is_set():
            _log(f"  [{slug}|{tid}] SKIPPED — rate limit breaker tripped")
            return None

        out_file = model_dir / f"{tid}.json"
        # Resume: skip if already completed successfully
        if out_file.exists():
            existing = json.loads(out_file.read_text(encoding="utf-8"))
            if not existing.get("errors") or len(existing.get("stages", {})) == 4:
                _log(f"  [{slug}|{tid}] Skipping (already done)")
                return existing

        raw = jira_data.get(tid, {})
        if not raw:
            _log(f"  [{slug}|{tid}] No JIRA data — skipping")
            return None

        conf_pages = conf_data.get(tid, [])

        try:
            result = run_ticket_pipeline(
                ticket_id=tid,
                raw_jira=raw,
                conf_pages=conf_pages,
                model_config=model_config,
                llm=llm,
                retriever=retriever,
                kt_sections=kt_sections,
                out_dir=model_dir,
            )
        except RateLimitBreaker as rl:
            rate_limit_hit.set()
            _log(f"\n  !!! RATE LIMIT HIT for [{slug}|{tid}]: {rl}")
            _log(f"  !!! STOPPING ALL PROCESSING for model {slug}")
            return None
        except Exception as e:
            _log(f"  [{slug}|{tid}] UNHANDLED ERROR: {e}")
            result = {"ticket_id": tid, "model": slug, "errors": [str(e)], "stages": {}}

        out_file.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        return result

    sem = threading.Semaphore(MAX_PARALLEL_TICKETS)

    def _run_guarded(tid):
        if rate_limit_hit.is_set():
            return None
        with sem:
            return _run_one(tid)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_TICKETS) as pool:
        futures = {pool.submit(_run_guarded, tid): tid for tid in TARGET_JIRA_IDS}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                res = future.result()
                if res:
                    with results_lock:
                        all_results.append(res)
            except Exception as e:
                _log(f"  [{slug}|{tid}] Future error: {e}")
                if isinstance(e, RateLimitBreaker):
                    rate_limit_hit.set()
                    break

    if rate_limit_hit.is_set():
        _log(f"\n  [{slug}] ABORTED — rate limit hit. {len(all_results)} tickets completed before abort.")
    else:
        _log(f"\n  [{slug}] Completed {len(all_results)}/{len(TARGET_JIRA_IDS)} tickets.")
    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
# COMPARISON REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def generate_comparison_report(all_model_results: dict[str, list[dict]]) -> str:
    """Generate a markdown comparison report across all models."""
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# IDRE Artoo — Multi-Model Benchmark Comparison",
        f"\n**Generated:** {ts}",
        f"**Tickets:** {len(TARGET_JIRA_IDS)} (last 30 by Karthick Murugan & Akshay Bhardwaj)",
        f"**Models:** {', '.join(m['slug'] for m in MODELS)}",
        "\n---\n",
    ]

    # ── Summary table ──────────────────────────────────────────────────────────
    lines.append("## Summary — Average Scores Across All Tickets\n")
    header = "| Metric |"
    sep    = "|--------|"
    for m in MODELS:
        header += f" {m['slug']} |"
        sep    += ":---:|"
    lines.extend([header, sep])

    def _avg(results: list[dict], key_path: list[str]) -> float:
        vals = []
        for r in results:
            node = r
            for k in key_path:
                node = node.get(k, {}) if isinstance(node, dict) else {}
            if isinstance(node, (int, float)):
                vals.append(node)
        return sum(vals) / len(vals) if vals else 0.0

    def _count(results: list[dict], key_path: list[str]) -> float:
        vals = []
        for r in results:
            node = r
            for k in key_path:
                node = node.get(k, {}) if isinstance(node, dict) else {}
            if isinstance(node, list):
                vals.append(len(node))
        return sum(vals) / len(vals) if vals else 0.0

    metrics = [
        ("Completeness Score",  ["stages", "completeness", "score"]),
        ("Plan Steps (avg)",    ["stages", "planning", "steps"]),
        ("Files Proposed (avg)",["stages", "code_proposal", "files_changed"]),
        ("Test Cases (avg)",    ["stages", "test_suggestions", "test_cases"]),
    ]

    for label, path in metrics:
        row = f"| {label} |"
        for m in MODELS:
            results = all_model_results.get(m["slug"], [])
            if path[-1] in ("steps", "files_changed", "test_cases"):
                val = _count(results, path)
                row += f" {val:.1f} |"
            else:
                val = _avg(results, path)
                row += f" {val:.0%} |" if path[-1] == "score" else f" {val:.2f} |"
        lines.append(row)

    # Error rate row
    row = "| Error Rate |"
    for m in MODELS:
        results = all_model_results.get(m["slug"], [])
        if not results:
            row += " N/A |"
            continue
        errors = sum(1 for r in results if r.get("errors"))
        row += f" {errors/len(results):.0%} ({errors}/{len(results)}) |"
    lines.append(row)

    # ── Per-ticket breakdown ───────────────────────────────────────────────────
    lines.append("\n---\n## Per-Ticket Results\n")

    # Build a per-ticket comparison table
    col_heads = "| Ticket | Assignee |"
    col_sep   = "|--------|---------|"
    for m in MODELS:
        col_heads += f" {m['slug']} Compl. | {m['slug']} Plan | {m['slug']} Files |"
        col_sep   += ":---:|:---:|:---:|"
    lines.extend([col_heads, col_sep])

    for tid in TARGET_JIRA_IDS:
        row = f"| {tid} |"
        assignee = ""
        for m in MODELS:
            results = all_model_results.get(m["slug"], [])
            res = next((r for r in results if r.get("ticket_id") == tid), None)
            if not assignee and res:
                assignee = (res.get("assignee") or "?").split()[-1]
            if not res:
                row_cell = " — | — | — |"
            else:
                compl = res.get("stages", {}).get("completeness", {}).get("score")
                plan  = len(res.get("stages", {}).get("planning", {}).get("steps", []))
                files = len(res.get("stages", {}).get("code_proposal", {}).get("files_changed", []))
                errs  = len(res.get("errors", []))
                compl_str = f"{compl:.0%}" if isinstance(compl, float) else "ERR"
                row_cell = f" {compl_str} | {plan} | {files}{'⚠' if errs else ''} |"
            row += row_cell
        lines.append(f"| {tid} | {assignee} |" + row[len(f"| {tid} |"):])

    # ── Per-model detail ───────────────────────────────────────────────────────
    lines.append("\n---\n## Per-Model Detail\n")
    for m in MODELS:
        slug    = m["slug"]
        results = all_model_results.get(slug, [])
        lines.append(f"### {slug} ({m['model_id']})\n")
        lines.append(f"**Provider:** {m['provider']}  |  **Tickets run:** {len(results)}/30\n")

        ok      = [r for r in results if not r.get("errors")]
        errored = [r for r in results if r.get("errors")]

        if ok:
            comp_scores = [r["stages"]["completeness"]["score"]
                           for r in ok
                           if isinstance(r.get("stages", {}).get("completeness", {}).get("score"), float)]
            plan_steps  = [len(r["stages"].get("planning", {}).get("steps", [])) for r in ok]
            file_counts = [len(r["stages"].get("code_proposal", {}).get("files_changed", [])) for r in ok]
            test_counts = [len(r["stages"].get("test_suggestions", {}).get("test_cases", [])) for r in ok]

            def _safe_avg(lst):
                return sum(lst) / len(lst) if lst else 0

            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Avg Completeness | {_safe_avg(comp_scores):.0%} |")
            lines.append(f"| Avg Plan Steps   | {_safe_avg(plan_steps):.1f} |")
            lines.append(f"| Avg Files Proposed | {_safe_avg(file_counts):.1f} |")
            lines.append(f"| Avg Test Cases   | {_safe_avg(test_counts):.1f} |")
            lines.append(f"| Success Rate     | {len(ok)/len(results):.0%} |")
            lines.append("")

        if errored:
            lines.append(f"**Errors ({len(errored)} tickets):**")
            for r in errored[:5]:
                lines.append(f"- {r['ticket_id']}: {'; '.join(r['errors'])[:100]}")
            lines.append("")

        # Show sample output for first successful ticket
        if ok:
            sample = ok[0]
            lines.append(f"**Sample: {sample['ticket_id']} — {sample.get('title','')[:60]}**\n")
            plan_s = sample.get("stages", {}).get("planning", {})
            if plan_s.get("steps"):
                lines.append("Plan steps:")
                for s in plan_s["steps"][:4]:
                    files_str = ", ".join(f"`{f}`" for f in s.get("affected_files", [])[:2])
                    lines.append(f"  {s['step']}. **{s['title']}** {files_str}")
            code_s = sample.get("stages", {}).get("code_proposal", {})
            if code_s.get("files_changed"):
                lines.append("\nFiles proposed:")
                for fc in code_s["files_changed"][:5]:
                    lines.append(f"  - `{fc['file']}` ({fc['type']})")
            lines.append("")
        lines.append("---\n")

    lines.append("## Notes\n")
    lines.append("- Results saved in `multi_model_results/{model_slug}/{IDRE-XXX}.json`")
    lines.append("- Confluence context fetched from IDRE SD space (read-only)")
    lines.append("- Codebase RAG: ChromaDB (BAAI/bge-large-en-v1.5)")
    lines.append("- KT context: IDRE platform knowledge transfer docs (videos + PDFs)")
    lines.append("- All JIRA data cached in `multi_model_results/jiras/`")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  IDRE Artoo — Multi-Model Benchmark")
    print(f"  Models: {', '.join(m['slug'] for m in MODELS)}")
    print(f"  Tickets: {len(TARGET_JIRA_IDS)}")
    print("  Mode: READ-ONLY (no JIRA/Confluence writes)")
    print("=" * 70)

    # ── Phase 1A: Fetch JIRAs ─────────────────────────────────────────────────
    jira_data = fetch_all_jiras()

    # ── Phase 1B: Fetch Confluence ────────────────────────────────────────────
    conf_data = fetch_all_confluence(jira_data)

    # ── Load KT document sections ─────────────────────────────────────────────
    kt_sections = load_kt_sections()
    _log(f"[KT] Loaded {len(kt_sections)} sections from KT summary.\n")

    # ── Load RAG retriever (ChromaDB, loaded once, shared across all models) ──
    _log("[RAG] Initialising ChromaDB retriever...")
    try:
        from rag_retriever_improved import get_improved_retriever
        retriever = get_improved_retriever()
    except Exception as e:
        _log(f"  WARN: Could not load improved retriever ({e}). Trying basic retriever...")
        try:
            from rag_retriever import IDRERetriever
            retriever = IDRERetriever()
        except Exception as e2:
            _log(f"  ERROR: No retriever available: {e2}. Proceeding without codebase RAG.")
            retriever = None

    # ── Phase 2: Run each model ───────────────────────────────────────────────
    all_model_results: dict[str, list[dict]] = {}
    benchmark_start = time.time()

    benchmark_aborted = False
    for i, model_config in enumerate(MODELS, 1):
        slug = model_config["slug"]
        _log(f"\n[{i}/{len(MODELS)}] Starting model: {slug}")

        # Reset circuit breaker for each new model (different provider = different limits)
        circuit_breaker.reset()

        model_start = time.time()
        results = run_model_batch(
            model_config=model_config,
            jira_data=jira_data,
            conf_data=conf_data,
            retriever=retriever,
            kt_sections=kt_sections,
        )
        all_model_results[slug] = results
        elapsed = time.time() - model_start
        _log(f"\n  [{slug}] Done in {elapsed:.0f}s  ({len(results)} tickets)")

        # Save intermediate per-model summary
        model_dir = RESULTS_ROOT / slug
        model_dir.mkdir(exist_ok=True)
        summary_file = model_dir / "_summary.json"
        summary_file.write_text(
            json.dumps({
                "model": model_config,
                "ticket_count": len(results),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "results": results,
                "rate_limit_hit": circuit_breaker.is_tripped,
            }, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )

        if circuit_breaker.is_tripped:
            _log(f"\n  !!! BENCHMARK STOPPING — rate limit hit for {slug}.")
            _log(f"  !!! Remaining models will NOT be run to avoid burning quota.")
            _log(f"  !!! Fix: increase rate limit delays, wait for quota reset, or switch provider.")
            benchmark_aborted = True
            break

    total_elapsed = time.time() - benchmark_start
    _log(f"\n{'='*70}")
    _log(f"  All {len(MODELS)} models completed in {total_elapsed/60:.1f} minutes")
    _log(f"{'='*70}")

    # ── Phase 3: Generate comparison report ───────────────────────────────────
    _log("\n[Phase 3] Generating comparison report...")
    report = generate_comparison_report(all_model_results)
    report_path = RESULTS_ROOT / "comparison_report.md"
    report_path.write_text(report, encoding="utf-8")
    _log(f"  Report saved -> {report_path}")

    # Also save combined JSON for later analysis
    combined_path = RESULTS_ROOT / "all_results.json"
    combined_path.write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_elapsed_minutes": round(total_elapsed / 60, 1),
            "models": MODELS,
            "tickets": TARGET_JIRA_IDS,
            "results": all_model_results,
        }, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    _log(f"  Combined JSON -> {combined_path}")

    print("\n" + "=" * 70)
    print("  BENCHMARK COMPLETE")
    print(f"  Results: {RESULTS_ROOT}")
    print("=" * 70)


if __name__ == "__main__":
    main()
