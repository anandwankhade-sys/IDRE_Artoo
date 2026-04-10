# Artoo — Setup & Usage Guide

This guide walks you through setting up and running Artoo from scratch.

---

## What is Artoo?

Artoo is an AI-powered SDLC automation system that reads JIRA tickets and generates draft GitHub Pull Requests with code changes. It uses a 15-step LangGraph pipeline with multiple specialized agents to fetch tickets, analyze code, plan implementations, generate code, and create PRs.

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Runtime |
| Node.js | 18+ | Required by GitHub MCP server |
| npm | 9+ | Installs GitHub MCP server |
| uv (optional) | latest | Runs JIRA/Confluence MCP server via `uvx` |
| Git | 2.30+ | Version control |

### Accounts & Credentials Needed

| Service | What You Need | Where to Get It |
|---------|--------------|----------------|
| Google Gemini (or OpenAI/AWS Bedrock) | API Key | https://aistudio.google.com/apikey |
| Atlassian JIRA | Email + API Token | https://id.atlassian.com/manage-profile/security/api-tokens |
| Atlassian Confluence | Same as JIRA | (same token works) |
| GitHub | Personal Access Token (PAT) | https://github.com/settings/tokens |

**GitHub PAT Scopes Required:**
- `repo` (full control) — for reading source code and creating PRs
- `read:org` — if your repo is in an organization
- If using separate read/write repos, create two tokens:
  - Read token: `repo:read` on the source code repo
  - Write token: `repo` on the PR target repo

---

## Step 1: Clone & Create Virtual Environment

```bash
# Navigate to where you want the project
cd /path/to/your/projects

# If you received this as a folder, just cd into it:
cd "FINAL IDRE Artoo Shareable"

# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Verify Installation

```bash
python -c "import langgraph, langchain, streamlit, pydantic; print('All dependencies OK')"
```

---

## Step 2: Configure Environment

```bash
# Copy the example env file
cp .env.example .env
```

Open `.env` in your editor and fill in ALL values. Here's what each section needs:

### LLM Provider (pick one)

**Option A: Google Gemini (recommended)**
```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIzaSy...your-key-here
GEMINI_MODEL_ID=gemini-2.5-pro-preview-03-25
```

**Option B: OpenAI**
```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...your-key-here
OPENAI_MODEL_ID=gpt-4o
```

**Option C: AWS Bedrock**
```env
LLM_PROVIDER=bedrock
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
BEDROCK_MODEL_ID=us.anthropic.claude-3-5-haiku-20241022-v1:0
```

### JIRA

```env
JIRA_URL=https://your-company.atlassian.net
JIRA_USERNAME=your.email@company.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_PROJECTS_FILTER=YOUR_PROJECT_KEY    # e.g., PROJ, IDRE, SCRUM
```

### Confluence

```env
CONFLUENCE_URL=https://your-company.atlassian.net/wiki
CONFLUENCE_SPACE_KEYS=YOUR_SPACE          # e.g., SD, ENG, DOCS
```

### GitHub

```env
# Source repo (where the production code lives — Artoo READS from here)
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
GITHUB_REPO_OWNER=your-org
GITHUB_REPO_NAME=your-repo
GITHUB_BASE_BRANCH=main

# PR target repo (where Artoo WRITES draft PRs — can be same or different repo)
GITHUB_PR_TOKEN=ghp_...
GITHUB_PR_REPO_OWNER=your-org
GITHUB_PR_REPO_NAME=your-repo
```

### Safety (start with dry run!)

```env
DRY_RUN=true           # Start with this! No PRs created, no JIRA comments
JIRA_READ_ONLY=true    # No writes to JIRA
```

---

## Step 3: Build the Knowledge Base (First Time Only)

The Knowledge Base (KB) is a set of pre-analyzed data about your codebase. It lives in `code_intelligence/data/`. If you're using this on a **new repo**, you need to build the KB first:

```bash
# Build all KB files (takes 10-30 minutes depending on repo size)
python helper_scripts/setup_knowledge_base.py
```

This creates:
- `file_summaries.json` — Per-file summaries, exports, imports, key concepts
- `repo_map.txt` — Directory tree with function/class names
- `co_change.json` — Files that change together in git history
- `module_keywords.json` — Domain keyword to module mapping
- `scope_baselines.json` — Average files changed per ticket type

**If you already have KB files** (included in this distribution), you can skip this step. The existing KB is for the IDRE project codebase.

### Validate KB

```bash
python helper_scripts/validate_knowledge_base.py
```

### Rebuild Individual KB Components

```bash
# Rebuild file summaries (LLM-powered, slow)
python helper_scripts/rebuild_file_summaries_simple.py

# Rebuild repo map (fast, no LLM)
python helper_scripts/build_repo_map.py

# Rebuild co-change patterns (fast, no LLM)
python helper_scripts/build_co_change.py

# Rebuild module keywords (fast, no LLM)
python helper_scripts/build_module_keywords.py
```

---

## Step 4: Initialize Database

```bash
python -c "from persistence.database import init_db; init_db(); print('Database initialized')"
```

This creates `data/artoo.db` (SQLite) with all required tables.

---

## Step 5: Test Your Setup

### Quick Connection Test

```bash
# Test that MCP servers can connect
python -c "
import asyncio
from mcp_client.client_factory import get_mcp_client, filter_jira_tools

async def test():
    async with get_mcp_client() as client:
        tools = await client.get_tools()
        print(f'Connected! {len(tools)} MCP tools available')

asyncio.run(test())
"
```

### Dry Run a Single Ticket

```bash
python main.py --mode single --ticket YOUR-TICKET-ID --dry-run
```

This runs the full pipeline but skips JIRA comments and PR creation. Check the output for errors.

---

## Step 6: Run Artoo

### Process a Single Ticket

```bash
# Dry run (safe — no side effects)
python main.py --mode single --ticket PROJ-123 --dry-run

# Live run (creates a draft PR on GitHub)
python main.py --mode single --ticket PROJ-123
```

### Process Multiple Tickets

Edit `helper_scripts/run_batch_30.py` (or create your own) with your ticket IDs:

```python
TICKETS = ["PROJ-1", "PROJ-2", "PROJ-3"]
```

Then run:

```bash
python helper_scripts/run_batch_30.py
```

### Automatic Polling Mode (Production)

Polls JIRA every 5 minutes for new "Ready for Dev" tickets:

```bash
python main.py --mode scheduler
```

---

## Step 7: Launch the Dashboard

### Streamlit Dashboard (Workflow Monitor)

```bash
streamlit run dashboard.py --server.port 8502
```

Open http://localhost:8502 in your browser.

**Tabs:**
- **Workflow Runs** — All ticket processing runs, status, completeness scores, PR links
- **LLM Call Log** — Per-agent LLM call details: tokens, latency, success rate
- **Human Review** — PR approval/rejection tracking for KPI measurement

### Metrics API Server

```bash
python main.py --mode metrics-server
```

Runs on http://localhost:8080. Endpoints:
- `GET /health` — Health check
- `GET /metrics` — KPI dashboard (JSON)
- `POST /pr/{run_id}/approve` — Mark PR as approved
- `POST /pr/{run_id}/reject` — Mark PR as rejected

### KPI Report (Terminal)

```bash
python main.py --mode metrics
```

### Full Demo Mode (Everything at Once)

```bash
python main.py --mode demo
```

Launches metrics API + Streamlit dashboard + log viewer simultaneously.

---

## Pipeline Overview

```
JIRA Ticket
    |
    v
[1] Fetch Ticket ──── JIRA MCP
[2] Classify ──────── Deterministic (bug/feature/ui/refactor)
[3] Assemble Context ─ Static Knowledge Base lookup
[4] Scout Repo ─────── LLM + GitHub MCP
[5] Confluence Docs ── Confluence MCP + LLM
[6] Completeness ───── LLM scoring (threshold: 0.25)
    |
    ├── [Incomplete] → Post clarification to JIRA → END
    |
    └── [Complete] → [7] Explore Code (LLM + GitHub MCP)
                     [8] Plan Implementation (LLM)
                     [9] Critique Plan (deterministic)
                     [10] Check Scope (deterministic)
                     [11] Generate Code (LLM)
                     [12] Validate Output (deterministic)
                     [13] Fix File Paths (deterministic)
                     [14] Suggest Tests (LLM)
                     [15] Create PR (GitHub MCP)
                         |
                         v
                     Draft PR on GitHub
```

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | LLM backend: `gemini`, `openai`, or `bedrock` |
| `COMPLETENESS_THRESHOLD` | `0.25` | Minimum score to proceed (0.0-1.0) |
| `REPO_SCOUT_MAX_FILES` | `20` | Max files repo scout returns |
| `LLM_PARSE_RETRY_COUNT` | `3` | Retries on LLM parse failure |
| `DRY_RUN` | `true` | Disable all writes (JIRA + GitHub) |
| `JIRA_READ_ONLY` | `true` | Disable JIRA writes only (PRs still created) |
| `SQLITE_DB_PATH` | `data/artoo.db` | SQLite database location |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Helper Scripts Reference

| Script | Purpose | Usage |
|--------|---------|-------|
| `run_batch_30.py` | Process batch of tickets | Edit ticket list, then `python helper_scripts/run_batch_30.py` |
| `run_single_ticket_hybrid.py` | Single ticket test | `python helper_scripts/run_single_ticket_hybrid.py PROJ-123` |
| `compare_pr_to_actual.py` | Compare Artoo PRs vs dev PRs | `python helper_scripts/compare_pr_to_actual.py` |
| `fetch_latest_tickets.py` | List recent completed tickets | `python helper_scripts/fetch_latest_tickets.py` |
| `setup_knowledge_base.py` | Build KB from scratch | `python helper_scripts/setup_knowledge_base.py` |
| `validate_knowledge_base.py` | Verify KB integrity | `python helper_scripts/validate_knowledge_base.py` |
| `multi_model_benchmark.py` | Compare LLM models | `python helper_scripts/multi_model_benchmark.py` |

---

## Troubleshooting

### MCP Server Won't Start
```
ImportError: cannot import name 'FakeConnection' from 'fakeredis.aioredis'
```
**Fix:** The `mcp-atlassian` package has a dependency conflict. Run:
```bash
uvx mcp-atlassian --version
```
If it shows 0.21+, add this alias to the `fakeredis/aioredis.py` in the uv cache:
```python
FakeConnection = FakeAsyncRedisConnection
```

### `$defs` Schema Warnings
```
Key '$defs' is not supported in schema, ignoring
```
**Safe to ignore.** This is a cosmetic warning from `langchain-mcp-adapters` not supporting JSON Schema 2019-09+. Tools work correctly.

### LLM Returns 0% Completeness
Check that `COMPLETENESS_THRESHOLD` is set to `0.25` in `.env`. Also verify the ticket has at least a title (not blank).

### GitHub PR Creation Fails
1. Verify `GITHUB_PR_TOKEN` has `repo` scope
2. Verify `GITHUB_PR_REPO_OWNER` and `GITHUB_PR_REPO_NAME` are correct
3. Check that the target repo exists and the token has write access

### Dashboard Won't Load
```bash
# Kill any existing Streamlit process
taskkill /F /IM streamlit.exe  # Windows
# or
pkill streamlit  # macOS/Linux

# Relaunch
streamlit run dashboard.py --server.port 8502
```

---

## Project Structure

See `COMPLETE_ARCHITECTURE.md` for the full architecture documentation including:
- Every agent's inputs, outputs, and LLM usage
- Full LangGraph DAG with conditional routing
- Database schema details
- MCP integration details
- All Pydantic schemas

See `ISSUES.md` for known issues and their resolution status.
