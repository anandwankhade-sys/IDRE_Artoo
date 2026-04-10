# IDRE Artoo — Complete Architecture & Workflow

**Last Updated:** 2026-04-10
**Version:** Hybrid Pipeline (final/)
**Purpose:** Transform JIRA tickets into draft GitHub Pull Requests using a multi-agent LangGraph pipeline

---

## Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│  IDRE ARTOO HYBRID PIPELINE                                                │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│                                                                            │
│  Input:  JIRA Ticket ID                                                    │
│  Output: Draft PR on GitHub with AI-generated code changes                │
│                                                                            │
│  Key Design Principle: Static Knowledge Base + Dynamic Agentic Exploration │
│  No RAG / no embedding vectors — deterministic KB lookup + LLM reasoning  │
│                                                                            │
│  LLM: Google Gemini 2.5 Pro Preview (configurable: Bedrock / OpenAI)      │
│  MCP: uvx mcp-atlassian (JIRA + Confluence), npx server-github (GitHub)   │
│  DB:  SQLite (data/artoo.db)                                               │
│  UI:  Streamlit dashboard (port 8502)                                      │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Repository Layout

```
final/
├── agents/                        # 17 agent modules + orchestration supervisor
│   ├── base_agent.py              # Abstract base: LLM invocation, retries, logging
│   ├── supervisor.py              # LangGraph graph definition + run_workflow()
│   ├── ticket_fetcher.py          # Fetch JIRA ticket via MCP
│   ├── ticket_classifier.py       # Classify ticket type (deterministic)
│   ├── context_assembler.py       # Assemble Knowledge Base context (deterministic)
│   ├── repo_scout_agent.py        # Identify relevant repo files (LLM + GitHub MCP)
│   ├── confluence_agent.py        # Fetch Confluence docs (MCP + LLM)
│   ├── completeness_agent.py      # Validate ticket completeness (LLM)
│   ├── explorer_agent.py          # Deep-read chosen files (LLM + GitHub MCP)
│   ├── planner_agent.py           # Generate implementation plan (LLM)
│   ├── plan_critic.py             # Review plan quality (deterministic)
│   ├── scope_calibrator.py        # Check plan scope vs baseline (deterministic)
│   ├── code_proposal_agent.py     # Generate code changes (LLM)
│   ├── validation_agent.py        # Validate proposal gates (deterministic)
│   ├── file_validator_agent.py    # Correct hallucinated file paths (deterministic)
│   ├── test_agent.py              # Suggest test cases (LLM)
│   └── pr_composer_agent.py       # Create GitHub PR via MCP
│
├── schemas/                       # Pydantic v2 data models
│   ├── workflow_state.py          # WorkflowState TypedDict + WorkflowPhase enum
│   ├── ticket.py                  # TicketContext, JiraAttachment
│   ├── plan.py                    # ImplementationPlan, ImplementationStep
│   ├── code_proposal.py           # CodeProposal, FileDiff, ChangeType
│   ├── pr.py                      # PRCompositionResult, PRStatus
│   ├── repo.py                    # RepoContext, FileAnalysis
│   ├── completeness.py            # CompletenessResult, MissingField, CompletenessDecision
│   ├── confluence.py              # ConfluenceContext, ConfluencePage
│   └── test_suggestion.py         # TestSuggestions, TestCase, TestType
│
├── prompts/                       # LLM prompt templates (SYSTEM + HUMAN pairs)
│   ├── planner_prompt.py
│   ├── code_proposal_prompt.py
│   ├── completeness_prompt.py
│   ├── test_prompt.py
│   ├── repo_scout_prompt.py
│   ├── confluence_prompt.py
│   └── pr_composer_prompt.py
│
├── config/
│   ├── settings.py                # Pydantic BaseSettings — all env vars
│   └── logging_config.py          # Structlog structured JSON logging
│
├── mcp_client/
│   └── client_factory.py          # MCP server configs, context managers, tool filters
│
├── llm/
│   ├── provider.py                # LLM provider factory (Bedrock / OpenAI / Gemini)
│   ├── bedrock_client.py          # AWS Bedrock specific client
│   └── llm_logger.py              # Per-call LLM logging to DB + JSONL
│
├── persistence/
│   ├── models.py                  # SQLAlchemy ORM: TicketRun, LLMCallLog, etc.
│   ├── database.py                # Engine + session factory
│   └── repository.py             # TicketRepository CRUD layer
│
├── code_intelligence/             # Static Knowledge Base (pre-built, read-only at runtime)
│   ├── knowledge_base.py          # KB loader used by context_assembler
│   ├── builder.py                 # KB construction orchestration
│   ├── git_analyzer.py            # Git history → co-change patterns
│   ├── file_summarizer.py         # Per-file LLM summarization
│   ├── repo_map.py                # Directory + function/class extraction
│   └── data/
│       ├── file_summaries.json    # ~1.2 MB: per-file summaries, exports, key_concepts
│       ├── repo_map.txt           # ~55 KB: directory tree with functions/classes
│       ├── co_change.json         # ~20 KB: file pairs that change together in git
│       ├── scope_baselines.json   # Historical avg files changed per ticket type
│       └── module_keywords.json   # Domain keywords → module mapping
│
├── app_logging/
│   └── activity_logger.py         # Structured activity logger (wraps structlog)
│
├── utils/
│   ├── file_index.py              # File path validation + hallucination detection
│   ├── mcp_helpers.py             # MCP tool utility functions
│   ├── retry.py                   # Retry + circuit breaker decorators
│   ├── markdown_parser.py         # Parse LLM markdown → CodeProposal
│   ├── sanitizer.py               # PII redaction from ticket text
│   └── text_helpers.py            # Text processing utilities
│
├── scheduler/
│   ├── poller.py                  # APScheduler: poll JIRA for new "Ready for Dev" tickets
│   └── pr_reconciler.py           # Reconcile PR outcomes (merged/rejected) back to DB
│
├── metrics/
│   ├── poc_metrics.py             # KPI computation (KPI 1/2/3)
│   ├── label_ticket.py            # CLI tool for ground-truth labeling
│   └── server.py                  # FastAPI metrics HTTP server (port 8080)
│
├── helper_scripts/                # Operational + evaluation scripts
│   ├── run_batch_30.py            # Run first 30 tickets (15 Karthick + 15 Akshay)
│   ├── run_batch_60.py            # Run next 60 tickets (30 Karthick + 30 Akshay)
│   ├── run_single_ticket_hybrid.py
│   ├── compare_pr_to_actual.py    # Compare Artoo PRs vs actual dev PRs → Excel report
│   ├── fetch_latest_tickets.py    # Pull latest completed tickets from JIRA REST API
│   ├── build_repo_map.py          # Build repo_map.txt from GitHub
│   ├── build_co_change.py         # Build co_change.json from git history
│   ├── build_module_keywords.py   # Build module_keywords.json from file_summaries
│   ├── rebuild_file_summaries.py  # Full LLM re-summarization of all repo files
│   ├── rebuild_file_summaries_simple.py
│   ├── setup_knowledge_base.py    # First-time KB build orchestrator
│   ├── validate_knowledge_base.py # Verify KB integrity
│   ├── run_benchmark_20.py        # KPI benchmark on 20 tickets
│   ├── multi_model_benchmark.py   # Compare Bedrock / OpenAI / Gemini on 40 tickets
│   ├── test_github_api.py         # Smoke-test GitHub MCP connection
│   └── test_commit_details.py     # Test commit detail fetching
│
├── dashboard.py                   # Streamlit dashboard (port 8502)
├── main.py                        # CLI entry point
├── requirements.txt
├── .env                           # Live config (git-ignored)
├── .env.example                   # Template
├── data/artoo.db                  # SQLite database
└── logs/
    ├── activity.jsonl             # Structured pipeline activity logs
    └── llm_calls.jsonl            # Per-LLM-call logs
```

---

## LangGraph Workflow — Full DAG

```
START
  │
  ▼
[1. fetch_ticket]           ← JIRA MCP (or local cache)
  │
  ▼
[2. ticket_classifier]      ← Deterministic keyword rules
  │
  ▼
[3. context_assembler]      ← Static KB lookup (NO LLM)
  │
  ▼
[4. repo_scout]             ← LLM + GitHub MCP
  │
  ▼
[5. confluence_docs]        ← Confluence MCP + LLM
  │
  ▼
[6. completeness_check]     ← LLM structured output
  │
  ├─ [INCOMPLETE] ──→ [post_clarification] ──→ END
  │                    (posts JIRA comment, applies label)
  │
  └─ [COMPLETE] ──→ [7. explorer]        ← LLM + GitHub MCP file reads
                         │
                         ▼
                    [8. planner]          ← LLM structured output
                         │
                         ▼
                    [9. plan_critic]      ← Deterministic quality checks
                         │
                         ▼
                    [10. scope_calibrator] ← Deterministic ratio check
                         │
                         ├─ [OUT OF SCOPE & revision < 2]
                         │        ↓
                         │   [bump_revision] ──→ (back to [8. planner])
                         │
                         └─ [IN SCOPE] ──→ [11. code_proposal]  ← LLM text output
                                                  │
                                                  ▼
                                           [12. validation]      ← Deterministic gates
                                                  │
                                                  ├─ [BLOCK & revision < 2]
                                                  │        ↓
                                                  │   [bump_revision] ──→ (back to [8. planner])
                                                  │
                                                  └─ [PROCEED / FLAG] ──→ [13. file_validator]
                                                                                 │
                                                                                 ▼
                                                                          [14. test_suggestion] ← LLM
                                                                                 │
                                                                                 ▼
                                                                          [15. pr_composer]  ← GitHub MCP
                                                                                 │
                                                                                 ▼
                                                                          [end_workflow]
                                                                                 │
                                                                                END
```

**Revision loop:** Plans can be revised up to 2 times (3 attempts total). Each bump increments `plan_revision_count` and feeds critic/validator feedback back into the planner prompt. On the 3rd attempt, the pipeline forces through regardless of gate status.

---

## Node-by-Node Reference

### Node 1 — Fetch Ticket
**File:** `agents/ticket_fetcher.py`
**LLM Calls:** 0
**External Calls:** JIRA MCP (`jira_get_issue`)

What it does:
- Checks local JSON cache first (`jira_cache/`, `multi_model_results/jiras/`)
- Falls back to JIRA MCP to fetch the ticket
- Converts Atlassian Document Format (ADF) → plain markdown
- Extracts acceptance criteria from custom field or description section
- Redacts PII from description/AC text
- Returns `TicketContext` with: `ticket_id`, `title`, `description`, `acceptance_criteria`, `labels`, `priority`, `story_points`, `reporter`, `assignee`, `status`, `attachments`, `linked_issues`, `components`

---

### Node 2 — Ticket Classifier
**File:** `agents/ticket_classifier.py`
**LLM Calls:** 0 (deterministic keyword matching)

Classification rules (priority order):
| Type | Trigger keywords |
|------|-----------------|
| `bug_fix` | fix, bug, error, broken, issue, not working, incorrect, wrong, failing, crash |
| `feature` | add, implement, create, new feature, build, support for |
| `ui_change` | display, show, button, dashboard, UI, screen, page, render, modal |
| `refactor` | refactor, cleanup, optimize, improve, restructure, rewrite |
| `other` | (fallback) |

The `ticket_type` flows into:
- Completeness check (bug_fix gets a completeness bypass — terse bug titles are expected)
- Scope calibrator (baselines differ per type: bug_fix avg 3 files, feature avg 7 files)

---

### Node 3 — Context Assembler
**File:** `agents/context_assembler.py`
**LLM Calls:** 0 (deterministic KB lookup)

**The Knowledge Base** lives in `code_intelligence/data/`:

```
file_summaries.json   (~1.2 MB)
  Per-file entries: {
    "path": "lib/actions/banking.ts",
    "summary": "Handles bank account creation and validation...",
    "exports": ["createBankAccount", "updateBankStatus"],
    "imports": ["prisma", "zod"],
    "key_concepts": ["banking", "validation", "account"]
  }

repo_map.txt   (~55 KB)
  Directory tree annotated with function/class names per file:
    lib/actions/banking.ts
      createBankAccount(data)
      updateBankStatus(id, status)
      deleteBankAccount(id)

co_change.json   (~20 KB)
  File pairs that changed together in git history:
    "lib/actions/banking.ts+components/Banking.tsx": 15
    (these two files appeared together in 15 commits)

scope_baselines.json   (~2 KB)
  Historical file-change stats per ticket type:
    bug_fix:  { avg: 3,  p75: 5  }
    feature:  { avg: 7,  p75: 12 }
    ui_change:{ avg: 4,  p75: 7  }

module_keywords.json
  Domain keyword → module mapping:
    "bank" → ["lib/actions/banking.ts", "components/Banking.tsx", ...]
    "org"  → ["lib/actions/organizations.ts", ...]
```

Assembly process:
1. Extract domain concepts from ticket (title + description keywords)
2. Map concepts → modules using `module_keywords.json`
3. Query `file_summaries.json` for relevant file entries
4. Pull matching sections from `repo_map.txt`
5. Look up co-change pairs for those files
6. Load scope baseline for detected ticket type
7. Truncate all sections to fit prompt budget

Returns `assembled_context` dict with: `file_summaries_section`, `repo_map_section`, `co_change_hints`, `scope_baseline`

---

### Node 4 — Repo Scout
**File:** `agents/repo_scout_agent.py`
**LLM Calls:** 1 (structured output → `RepoContext`)
**External Calls:** GitHub MCP (directory listing, file contents, package.json)

What it does:
- Fetches repo directory structure and key metadata files via GitHub MCP
- LLM analyzes ticket + repo tree to identify the most relevant files
- For each file, fetches partial content to infer language/patterns
- Returns `RepoContext` with: `primary_language`, `directory_summary`, `relevant_files` (with relevance scores), `existing_test_files`, `dependency_hints`, `code_style_hints`, `impacted_modules`

---

### Node 5 — Confluence Docs
**File:** `agents/confluence_agent.py`
**LLM Calls:** 1 (summary synthesis)
**External Calls:** Confluence MCP (search + page fetch)

What it does:
- Generates up to 5 search queries from the ticket text
- Executes queries via Confluence MCP against configured space keys
- LLM synthesizes a summary from all retrieved pages
- Returns `ConfluenceContext` with: `pages_found`, `total_pages_searched`, `summary`, `doc_update_suggestions`

---

### Node 6 — Completeness Check
**File:** `agents/completeness_agent.py`
**LLM Calls:** 1 (structured output → `CompletenessResult`)

Scoring thresholds:
| Score | Decision |
|-------|---------|
| 0.65 – 1.0 | COMPLETE → pipeline proceeds |
| 0.30 – 0.64 | BORDERLINE → proceeds, all missing fields noted |
| 0.10 – 0.29 | INCOMPLETE → pipeline stops, clarification posted |

Special behaviors:
- A ticket with any title is never scored 0.0 (floor: 0.15)
- `bug_fix` type tickets get a completeness bypass at the routing step — a descriptive bug title is sufficient to proceed even with a low score

If incomplete, `post_clarification_node` runs instead:
- Posts a structured JIRA comment listing missing fields and clarifying questions
- Applies `needs-clarification` label to the JIRA ticket
- Pipeline terminates

Returns `CompletenessResult`: `decision`, `completeness_score`, `missing_fields`, `clarification_questions`, `assumptions_summary`

---

### Node 7 — Explorer (Key Innovation)
**File:** `agents/explorer_agent.py`
**LLM Calls:** 1+ (structured plan) + file reads
**External Calls:** GitHub MCP (file content, search, commits)

What it does:
1. LLM receives ticket + assembled context and produces an `ExplorationPlan` (3–5 files to read + what to look for in each)
2. For each file: reads full content from GitHub, runs targeted greps, optionally fetches recent commits
3. Assembles a narrative exploration report
4. Returns `exploration_context` (string) passed to planner

```python
class ExplorationPlan:
    files: list[FileToExplore]   # [{file_path, what_to_look_for}]
    reasoning: str
```

This is where Artoo builds real understanding of the codebase before writing any code.

---

### Node 8 — Planner
**File:** `agents/planner_agent.py`
**LLM Calls:** 1 (structured output → `ImplementationPlan`)

Two prompt modes:
- **Hybrid** (default): uses assembled KB context + exploration report
- **Legacy** (fallback): uses RAG-style repo context only

On revision loops: prior plan critique and validation feedback is injected into the prompt.

Returns `ImplementationPlan`:
```python
{
  summary: str,
  impacted_components: list[str],
  implementation_steps: list[ImplementationStep],
  risk_level: low | medium | high,
  risk_rationale: str,
  deployment_considerations: list[str],
  breaking_changes: bool,
  database_migrations_required: bool,
  confidence_score: float,   # 0.0–1.0
  assumptions: list[str],
}
```

---

### Node 9 — Plan Critic
**File:** `agents/plan_critic.py`
**LLM Calls:** 0 (deterministic quality checks)

Checks performed:
| Check | Condition |
|-------|-----------|
| AC coverage | All acceptance criteria mapped to at least one step |
| File hallucination | All referenced files exist in the repo index |
| Over-engineering | Step count > 2× typical for ticket type |
| Vague steps | Steps containing only "Update logic", "Refactor", etc. |
| Type mismatch | Bug fix ticket with redesign/new-system steps |

Returns `plan_critique`:
```python
{
  approved: bool,
  overall_quality: "good" | "acceptable" | "poor",
  hallucinated_files: list[str],
  unnecessary_steps: list[str],
  feedback: str,
  confidence_adjustment: float,   # -1.0 to +0.1 applied to plan confidence
}
```

---

### Node 10 — Scope Calibrator
**File:** `agents/scope_calibrator.py`
**LLM Calls:** 0 (deterministic ratio check)

Logic:
- Looks up `scope_baselines.json` for the detected ticket type
- Computes `ratio = proposed_file_count / baseline_avg`
- **Threshold:** ratio > 2.5 → out of scope → triggers revision loop (if revisions remaining)

Returns `scope_check`:
```python
{
  within_scope: bool,
  ratio: float,
  proposed_count: int,
  baseline: {avg_files, min, max},
  warning: str | None,
}
```

---

### Node 11 — Code Proposal
**File:** `agents/code_proposal_agent.py`
**LLM Calls:** 1 (text output parsed via markdown parser)

The LLM generates a markdown-formatted code proposal. `utils/markdown_parser.py` extracts structured data.

Returns `CodeProposal`:
```python
{
  summary: str,
  file_changes: list[FileDiff],   # see below
  new_dependencies: list[str],
  configuration_changes: list[str],
  migration_scripts: list[str],
  confidence_score: float,
  caveats: list[str],
}

FileDiff {
  file_path: str,
  change_type: ChangeType,   # CREATE | MODIFY | DELETE | RENAME
  proposed_content: str,     # full file content or unified diff
  is_diff_format: bool,
  rationale: str,
}
```

---

### Node 12 — Validation
**File:** `agents/validation_agent.py`
**LLM Calls:** 0 (deterministic gates)

Gates applied in order:
| Gate | Threshold | Action |
|------|-----------|--------|
| File hallucination (MODIFY/DELETE only — CREATE excluded) | > 30% non-existent paths | `block` |
| File hallucination warning | 20–30% | `flag_for_review` |
| Confidence proceed | >= 0.65 | `proceed` |
| Confidence flag | >= 0.40 | `flag_for_review` |
| Confidence block | < 0.40 | `block` |

On `block` with revisions remaining → triggers revision loop back to planner.
On `proceed` or `flag_for_review` → advances to file_validator.

---

### Node 13 — File Validator
**File:** `agents/file_validator_agent.py`
**LLM Calls:** 0 (index-based path correction)

What it does:
- Skips `ChangeType.CREATE` files (they don't exist yet — that's intentional)
- For `MODIFY` / `DELETE` files: checks each path against the full repo file index
- Attempts fuzzy matching to correct typos/wrong paths
- Removes paths that can't be matched (hallucinations)
- Mutates `code_proposal.file_changes` in-place with corrections

Returns updated `code_proposal` + `file_validation_result` summary.

---

### Node 14 — Test Suggestion
**File:** `agents/test_agent.py`
**LLM Calls:** 1 (structured output → `TestSuggestions`)

Returns `TestSuggestions`:
```python
{
  framework: str,                       # pytest | jest | vitest | etc.
  suggested_test_file_paths: list[str],
  test_cases: list[TestCase],
  coverage_targets: list[str],
  confidence_score: float,
}

TestCase {
  test_name: str,
  test_type: unit | integration | contract | e2e,
  target_function_or_class: str,
  description: str,
  arrange: str,
  act: str,
  assert_description: str,
  edge_case: bool,
  mock_dependencies: list[str],
  sample_code: str | None,
}
```

---

### Node 15 — PR Composer
**File:** `agents/pr_composer_agent.py`
**LLM Calls:** 0–1 (PR title generation)
**External Calls:** GitHub MCP (create branch, commit files, create PR)

Process:
1. Build PR body from: plan summary, file changes, test suggestions, Confluence references, confidence scores, AI disclaimer
2. Create branch: `artoo/{ticket_id}-{slug}`
3. Commit each `FileDiff` as a file change
4. Create draft PR against base branch
5. Link to JIRA ticket in PR body
6. Request configured default reviewers

Returns `PRCompositionResult`:
```python
{
  status: created | failed | skipped,
  pr_url: str,
  pr_number: int,
  branch_name: str,
  base_branch: str,
  pr_title: str,
  draft: bool,
  reviewers_requested: list[str],
  labels_applied: list[str],
  jira_ticket_linked: bool,
  error_message: str | None,
}
```

---

## Schemas Reference

### WorkflowState (LangGraph state)
```python
class WorkflowState(TypedDict, total=False):
    # Identity
    run_id: str                          # UUID for this pipeline run
    ticket_id: str
    started_at: str

    # Agent outputs (accumulated as pipeline runs)
    ticket_context: TicketContext | None
    completeness_result: CompletenessResult | None
    repo_context: RepoContext | None
    confluence_context: ConfluenceContext | None
    implementation_plan: ImplementationPlan | None
    code_proposal: CodeProposal | None
    test_suggestions: TestSuggestions | None
    pr_result: PRCompositionResult | None

    # Hybrid pipeline intermediate results
    ticket_type: str | None
    assembled_context: dict | None
    exploration_context: str | None
    plan_critique: dict | None
    scope_check: dict | None
    validation_result: dict | None

    # Control flow
    current_phase: WorkflowPhase
    is_complete_ticket: bool | None
    should_stop: bool
    plan_revision_count: int             # 0, 1, or 2 — max 2 revisions

    # Append-only audit lists (LangGraph operator.add reducers)
    errors: list[str]
    llm_call_ids: list[str]
    mcp_tool_calls: list[dict]

    # Counters
    total_llm_calls: int
    total_tokens_used: int
    completed_at: str | None
```

### WorkflowPhase enum
```
FETCHING_TICKET → CLASSIFYING_TICKET → ASSEMBLING_CONTEXT →
SCOUTING_REPO → FETCHING_CONFLUENCE_DOCS → CHECKING_COMPLETENESS →
POSTING_CLARIFICATION → EXPLORING_CODE → PLANNING → CRITIQUING_PLAN →
CALIBRATING_SCOPE → PROPOSING_CODE → VALIDATING_OUTPUT →
SUGGESTING_TESTS → COMPOSING_PR → COMPLETED | FAILED
```

---

## MCP Integration

### Client Factory (`mcp_client/client_factory.py`)

Two client contexts:

**`get_mcp_client()`** — Main client (JIRA + GitHub + Confluence)
- JIRA/Confluence: `uvx mcp-atlassian` via stdio, authenticated with `JIRA_USERNAME` + `JIRA_API_TOKEN`
- GitHub: `npx @modelcontextprotocol/server-github` via stdio, authenticated with `GITHUB_PERSONAL_ACCESS_TOKEN`

**`get_pr_mcp_client()`** — PR-write-only client (GitHub only)
- Uses `GITHUB_PR_TOKEN` (separate token with write permissions to the PR target repo)
- Falls back to `GITHUB_PERSONAL_ACCESS_TOKEN` if PR token not set

**Tool filter functions:**
```python
filter_jira_tools(tools)       # keywords: jira, issue, comment, atlassian, project, transition
filter_github_tools(tools)     # keywords: github, repo, pull_request, create_pull, get_file,
                               #           create_or_update, search_code, push, branch, commit, content
filter_confluence_tools(tools) # keywords: confluence, page, space, wiki
```

> **Important:** `search_issues` tool is NOT returned by `filter_github_tools()` (keyword mismatch).
> To use it: `next((t for t in all_tools if t.name == "search_issues"), None)`
> Its parameter is `q` (not `query`).

### Configured Repositories
| Purpose | Repo | Token |
|---------|------|-------|
| Read source code (explorer, scout) | `YOUR_ORG/YOUR_REPO` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| Write PRs | `YOUR_ORG/YOUR_PR_REPO` | `GITHUB_PR_TOKEN` |

---

## Persistence Layer

### Database Models (`persistence/models.py`)

**TicketRun** — one record per pipeline execution
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Run ID (PK) |
| `ticket_id` | String | JIRA ticket key |
| `status` | String | RUNNING / COMPLETED_COMPLETE / COMPLETED_INCOMPLETE / FAILED |
| `current_phase` | String | Latest phase (live-updated after each node) |
| `started_at` | DateTime | |
| `completed_at` | DateTime | |
| `completeness_score` | Float | 0.0–1.0 from completeness agent |
| `ticket_deemed_incomplete` | Boolean | |
| `implementation_plan_generated` | Boolean | |
| `code_proposal_generated` | Boolean | |
| `tests_suggested` | Boolean | |
| `pr_url` | String | GitHub PR URL |
| `pr_number` | Integer | |
| `pr_branch` | String | |
| `pr_outcome` | String | PENDING / APPROVED / REJECTED / MERGED / NOT_CREATED |
| `total_duration_seconds` | Float | |
| `total_llm_calls` | Integer | |
| `total_tokens_used` | Integer | |
| `error_occurred` | Boolean | |
| `error_phase` | String | Phase where error occurred |
| `error_message` | Text | Full error string |
| `final_state_snapshot` | JSON | Complete WorkflowState snapshot |

**LLMCallLog** — one record per LLM invocation
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Call ID (PK) |
| `run_id` | UUID | FK → TicketRun |
| `ticket_id` | String | |
| `agent_name` | String | Which agent made the call |
| `model_id` | String | Full model identifier |
| `prompt_template_name` | String | |
| `prompt_token_count` | Integer | |
| `completion_token_count` | Integer | |
| `total_token_count` | Integer | |
| `latency_ms` | Float | Wall-clock ms |
| `parsed_successfully` | Boolean | Structured output parsed OK |
| `invoked_at` | DateTime | |
| `error_occurred` | Boolean | |
| `error_message` | Text | |

**ProcessedTicket** — deduplication cache
- `ticket_id` (PK), `first_seen_at`, `last_run_id`, `reprocess_requested`

**TicketGroundTruth** — manual KPI labels
- `ticket_id` (PK), `truly_incomplete`, `labeled_by`, `labeled_at`, `notes`

---

## Live Progress Tracking

`supervisor.py` wraps every LangGraph node with `_wrap_node()`:

```python
_NODE_PHASE_MAP = {
    "fetch_ticket":       WorkflowPhase.FETCHING_TICKET,
    "ticket_classifier":  WorkflowPhase.CLASSIFYING_TICKET,
    "context_assembler":  WorkflowPhase.ASSEMBLING_CONTEXT,
    "repo_scout":         WorkflowPhase.SCOUTING_REPO,
    "confluence_docs":    WorkflowPhase.FETCHING_CONFLUENCE_DOCS,
    "completeness_check": WorkflowPhase.CHECKING_COMPLETENESS,
    "explorer":           WorkflowPhase.EXPLORING_CODE,
    "planner":            WorkflowPhase.PLANNING,
    "plan_critic":        WorkflowPhase.CRITIQUING_PLAN,
    "scope_calibrator":   WorkflowPhase.CALIBRATING_SCOPE,
    "code_proposal":      WorkflowPhase.PROPOSING_CODE,
    "validation":         WorkflowPhase.VALIDATING_OUTPUT,
    "file_validator":     WorkflowPhase.VALIDATING_OUTPUT,
    "test_suggestion":    WorkflowPhase.SUGGESTING_TESTS,
    "pr_composer":        WorkflowPhase.COMPOSING_PR,
}
```

After each node completes, `_update_progress()` writes to `TicketRun`:
- `current_phase` — latest phase name
- Intermediate results: `completeness_score`, `implementation_plan_generated`, `code_proposal_generated`, `tests_suggested`, `pr_url`

The Streamlit dashboard queries the DB every 8 seconds and displays `current_phase` as a column in the Workflow Runs table.

---

## Streamlit Dashboard

**File:** `dashboard.py` — Launch: `streamlit run dashboard.py --server.port 8502`

### Tabs

**Workflow Runs**
- Table of all `TicketRun` records
- Columns: ticket_id, status, current_phase, completeness_score, plan/code/tests flags, PR URL, duration, tokens, error message
- Filters: status, phase, date range, errors-only toggle

**LLM Call Log**
- Table of all `LLMCallLog` records
- Columns: timestamp, ticket_id, agent, model (shortened), tokens, latency (seconds), parse success, error flag
- Summary metrics row: total calls, unique models, avg latency, total tokens
- Side-by-side charts: calls per agent, avg latency per agent
- Filters: agent, model, success/error, date range

**Human Review**
- Lists all PRs created by Artoo
- Links to GitHub PR for review
- Records approved/rejected outcomes back to DB via metrics API

### KPI Cards
| KPI | Target | Description |
|-----|--------|-------------|
| KPI 1 | ≥ 33% | PR Approval Rate |
| KPI 2 | ≥ 50% | Incomplete Ticket Detection accuracy |
| KPI 3 | ≥ 10 | Consecutive error-free runs |

Auto-refresh: enabled by default, 10-second interval (configurable 10–30s).

---

## Configuration (Environment Variables)

All settings are defined in `config/settings.py` as a Pydantic `BaseSettings` class loaded from `.env`.

### LLM Provider
```
LLM_PROVIDER=gemini                    # bedrock | openai | gemini
GEMINI_API_KEY=...
GEMINI_MODEL_ID=gemini-2.5-pro-preview-03-25
GEMINI_MAX_TOKENS=8192
GEMINI_TEMPERATURE=0.1
```

### AWS Bedrock (if LLM_PROVIDER=bedrock)
```
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
BEDROCK_MODEL_ID=us.anthropic.claude-3-5-haiku-20241022-v1:0
BEDROCK_MAX_TOKENS=8192
BEDROCK_TEMPERATURE=0.1
```

### OpenAI (if LLM_PROVIDER=openai)
```
OPENAI_API_KEY=...
OPENAI_MODEL_ID=gpt-4o
OPENAI_MAX_TOKENS=8192
OPENAI_TEMPERATURE=0.1
```

### JIRA + Confluence
```
JIRA_URL=https://YOUR_INSTANCE.atlassian.net
JIRA_USERNAME=...
JIRA_API_TOKEN=...
JIRA_PROJECTS_FILTER=IDRE
JIRA_READ_ONLY=true                    # true = no comments/labels written to JIRA
JIRA_POLL_JQL=project in (IDRE) AND status = "Ready for Dev" ORDER BY created DESC
JIRA_POLL_INTERVAL_SECONDS=300
CONFLUENCE_URL=https://YOUR_INSTANCE.atlassian.net/wiki
CONFLUENCE_SPACE_KEYS=SD
CONFLUENCE_MAX_PAGES=10
```

### GitHub
```
GITHUB_PERSONAL_ACCESS_TOKEN=...       # Read access to YOUR_ORG/YOUR_REPO
GITHUB_REPO_OWNER=OrchidSoftwareSolutions
GITHUB_REPO_NAME=idre
GITHUB_BASE_BRANCH=main
GITHUB_DEFAULT_REVIEWERS=             # Comma-separated usernames
GITHUB_PR_TOKEN=...                   # Write access to YOUR_ORG/YOUR_PR_REPO
GITHUB_PR_REPO_OWNER=YOUR_PR_REPO_OWNER
GITHUB_PR_REPO_NAME=demo_sdlc
```

### Persistence & Logging
```
SQLITE_DB_PATH=data/artoo.db
DB_ECHO=false
LOG_LEVEL=INFO
ACTIVITY_LOG_PATH=logs/activity.jsonl
LLM_LOG_PATH=logs/llm_calls.jsonl
```

### Agent Behavior
```
COMPLETENESS_THRESHOLD=0.35           # Minimum score to proceed (borderline allowed)
REPO_SCOUT_MAX_FILES=20
LLM_PARSE_RETRY_COUNT=3
DRY_RUN=false                         # true = no GitHub/JIRA writes at all
```

---

## BaseAgent — Core LLM Invocation

**File:** `agents/base_agent.py`

All 10 LLM-calling agents extend `BaseAgent` which provides:

```python
invoke_llm(system_prompt, human_prompt, run_id, ticket_id, prompt_template_name)
    → tuple[str, str]                  # (response_text, call_id)

invoke_llm_structured(system_prompt, human_prompt, output_schema, run_id, ticket_id, ...)
    → tuple[PydanticModel, str]        # (parsed_object, call_id)
```

**Structured output strategy by provider:**
- OpenAI → `json_schema` mode
- Bedrock / Gemini → `function_calling` mode with Pydantic schema

**JSON repair pipeline** (on parse failure):
1. Strip markdown code fences
2. Find JSON object boundaries
3. Add missing closing braces/brackets
4. Retry `LLM_PARSE_RETRY_COUNT` times with repair hints in prompt

**`run_async(coro)`** — bridges async MCP calls from synchronous LangGraph nodes, handles nested event loop detection (Windows `asyncio` compatibility).

---

## Helper Scripts

### Batch Processing
| Script | Tickets | Purpose |
|--------|---------|---------|
| `run_batch_30.py` | 15 Karthick + 15 Akshay | First evaluation batch |
| `run_batch_60.py` | 30 Karthick + 30 Akshay | Second evaluation batch |
| `run_single_ticket_hybrid.py` | 1 ticket | Single-ticket test run |

### Knowledge Base Maintenance
| Script | Purpose | Output |
|--------|---------|--------|
| `setup_knowledge_base.py` | First-time full KB build | All KB JSON files |
| `rebuild_file_summaries_simple.py` | Re-summarize repo files with LLM | `file_summaries.json` |
| `build_repo_map.py` | Rebuild directory tree with functions | `repo_map.txt` |
| `build_co_change.py` | Rebuild git co-change patterns | `co_change.json` |
| `build_module_keywords.py` | Rebuild domain keyword map | `module_keywords.json` |
| `validate_knowledge_base.py` | Verify KB integrity | Validation report |

### Evaluation & Comparison
| Script | Purpose | Output |
|--------|---------|--------|
| `compare_pr_to_actual.py` | Compare Artoo PRs vs actual dev PRs from GitHub | Excel report (Summary + File Details sheets) |
| `run_benchmark_20.py` | KPI benchmark on 20 tickets | KPI metrics |
| `multi_model_benchmark.py` | Compare Bedrock / OpenAI / Gemini | Per-model JSON results |
| `fetch_latest_tickets.py` | Pull completed tickets from JIRA REST API | JSON list |

### Debugging
| Script | Purpose |
|--------|---------|
| `test_github_api.py` | Smoke-test GitHub MCP connection |
| `test_commit_details.py` | Test commit detail fetching from GitHub |

---

## PR Comparison Tool

**File:** `helper_scripts/compare_pr_to_actual.py`

Compares Artoo's generated PRs against the real PRs developers submitted for the same JIRA tickets.

Process:
1. Loads all completed pipeline runs from SQLite DB
2. For each run with a generated PR, searches GitHub for developer PRs mentioning the ticket ID using `search_issues` (param: `q`, fetched from `all_tools` not `filter_github_tools`)
3. Fetches changed files from each PR via `get_pull_request_files`
4. Computes file overlap %: exact path match + basename match
5. Generates Excel report with two sheets:
   - **Summary**: per-ticket overlap %, PR URLs, file counts
   - **File Details**: file-level match breakdown

---

## Known Issues & Fixes Applied

| Issue | Root Cause | Fix Applied |
|-------|-----------|-------------|
| `ImportError: FakeConnection` from `fakeredis.aioredis` | `mcp-atlassian` v0.21 upgraded `fakeredis`; class renamed to `FakeAsyncRedisConnection` | Added alias `FakeConnection = FakeAsyncRedisConnection` at end of `fakeredis/aioredis.py` in uv cache |
| File validator false-positive on new files | `ChangeType.CREATE` files flagged as hallucinated because they don't exist in repo yet | Excluded `CREATE` type from both `validation_agent.py` and `file_validator_agent.py` checks |
| 0% completeness score for tickets with title only | LLM sometimes returns 0.0 when description is blank | Added programmatic floor: `if score == 0.0 and ticket.title: score = 0.15` |
| `KeyError: latency_ms not in index` in dashboard | Field renamed from `latency_ms` to `latency_s` in data loader but old Streamlit cache still used | Killed Streamlit process to clear cache; field now consistently named `latency_s` |
| `search_issues` returns no results | Tool not in `filter_github_tools()` result; wrong param name `query` used | Must fetch from `all_tools` directly; correct param is `q` |
| `name 'logger' is not defined` in explorer_agent | 4 bare `logger.warning()` calls inside class methods | Fixed to `self.logger.warning()` on lines 802, 821, 846, 865 |

---

## KPI Definitions

| KPI | Target | Measurement |
|-----|--------|------------|
| **KPI 1: PR Approval Rate** | ≥ 33% | Fraction of Artoo PRs approved/merged by developers |
| **KPI 2: Incomplete Detection** | ≥ 50% | Fraction of truly-incomplete tickets correctly flagged (requires ground-truth labels via `label_ticket.py`) |
| **KPI 3: Error-Free Runs** | ≥ 10 consecutive | Pipeline runs completing without a FAILED status |

---

## Pipeline Run Outcomes

A ticket run ends in one of four states:

| Status | Meaning |
|--------|---------|
| `COMPLETED_COMPLETE` | Ticket deemed complete + PR created (or skipped by validation) |
| `COMPLETED_INCOMPLETE` | Ticket deemed incomplete → clarification posted to JIRA, no PR |
| `FAILED` | Unhandled exception or agent error mid-pipeline |
| `RUNNING` | Currently in progress |

**Why OK count > PR count:** `COMPLETED_COMPLETE` includes tickets where validation blocked the code proposal after all 3 revision attempts. A PR is only created when the pipeline reaches `pr_composer_node` and GitHub MCP succeeds.

---

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `langgraph` | Graph-based multi-agent workflow orchestration |
| `langchain-core` | LLM abstractions, prompt templates |
| `langchain-mcp-adapters` | Translates MCP tool specs to LangChain tools |
| `mcp` | Model Context Protocol client |
| `langchain-google-genai` | Google Gemini integration |
| `langchain-aws` | AWS Bedrock integration |
| `langchain-openai` | OpenAI integration |
| `pydantic` / `pydantic-settings` | Data validation + env config |
| `SQLAlchemy` | ORM + SQLite persistence |
| `streamlit` | Dashboard UI |
| `structlog` | Structured JSON logging |
| `fastapi` + `uvicorn` | Metrics HTTP API |
| `APScheduler` | JIRA polling scheduler |
| `httpx` / `aiohttp` | Async HTTP |
| `tenacity` | Retry logic |
| `pandas` + `openpyxl` | Dashboard tables + Excel reports |
| `python-dotenv` | `.env` loading |
