# Artoo — AI-Powered SDLC Automation

Artoo reads JIRA tickets and generates draft GitHub Pull Requests with implementation code, test suggestions, and architecture documentation references. It uses a 15-step multi-agent pipeline built on LangGraph to analyze tickets, explore codebases, plan implementations, and create PRs — all without human intervention.

---

## How It Works

```
JIRA Ticket
    |
    v
 Fetch Ticket ──── JIRA MCP
 Classify ──────── bug / feature / ui / refactor
 Assemble Context ─ Static Knowledge Base (no RAG, no embeddings)
 Scout Repo ─────── LLM + GitHub MCP
 Confluence Docs ── Confluence MCP + LLM
 Completeness ───── LLM scoring (is there enough info to code?)
    |
    +-- [Incomplete] --> Post clarification comment on JIRA --> STOP
    |
    +-- [Complete] --> Explore Code (deep-reads relevant files)
                       Plan Implementation (step-by-step)
                       Critique Plan (quality gate)
                       Check Scope (proportionality gate)
                       Generate Code (per-file diffs)
                       Validate Output (hallucination check)
                       Fix File Paths (fuzzy correction)
                       Suggest Tests (AAA format)
                       Create PR (draft on GitHub)
                           |
                           v
                       Draft PR on GitHub
```

Plans that fail critique or scope checks loop back for revision (up to 2 retries).

---

## Key Features

- **15-step LangGraph pipeline** with conditional routing, revision loops, and quality gates
- **3 LLM providers**: Google Gemini, AWS Bedrock (Claude), OpenAI (GPT-4o)
- **MCP integration**: JIRA, Confluence, and GitHub via Model Context Protocol
- **Static Knowledge Base**: pre-analyzed file summaries, repo map, co-change patterns, scope baselines — no vector DB needed
- **Completeness gate**: rejects tickets that lack enough info and posts clarification questions back to JIRA
- **File validation**: catches hallucinated paths, fuzzy-matches misnamed files, skips new-file false positives
- **Co-change awareness**: uses git history to suggest files that typically change together
- **Layer detection**: guides the LLM to the correct architectural layer (service vs component vs webhook)
- **Streamlit dashboard**: live workflow monitoring, LLM call logs, KPI tracking
- **FastAPI metrics server**: PR approval rates, incomplete detection accuracy, error-free run streaks

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/anandwankhade-sys/IDRE_Artoo.git
cd IDRE_Artoo

# 2. Virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your credentials (JIRA, GitHub, LLM API key)

# 5. Initialize database
python -c "from persistence.database import init_db; init_db()"

# 6. Run a single ticket (dry run — no side effects)
python main.py --mode single --ticket PROJ-123 --dry-run

# 7. Run for real (creates a draft PR)
python main.py --mode single --ticket PROJ-123
```

---

## Run Modes

| Command | Description |
|---------|-------------|
| `python main.py --mode single --ticket PROJ-123` | Process one ticket end-to-end |
| `python main.py --mode single --ticket PROJ-123 --dry-run` | Same, but no JIRA/GitHub writes |
| `python main.py --mode scheduler` | Poll JIRA every 5 min for new tickets |
| `python main.py --mode metrics` | Print KPI report to terminal |
| `python main.py --mode metrics-server` | Start FastAPI metrics API (port 8080) |
| `python main.py --mode demo` | Launch everything: API + dashboard + log viewer |
| `streamlit run dashboard.py --server.port 8502` | Streamlit dashboard |

---

## Configuration

Copy `.env.example` to `.env` and fill in:

| Section | Variables |
|---------|-----------|
| **LLM** | `LLM_PROVIDER` (gemini/openai/bedrock), API key, model ID |
| **JIRA** | `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` |
| **Confluence** | `CONFLUENCE_URL`, `CONFLUENCE_SPACE_KEYS` |
| **GitHub (read)** | `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_REPO_OWNER`, `GITHUB_REPO_NAME` |
| **GitHub (write)** | `GITHUB_PR_TOKEN`, `GITHUB_PR_REPO_OWNER`, `GITHUB_PR_REPO_NAME` |
| **Safety** | `DRY_RUN=true` (start here), `JIRA_READ_ONLY=true` |

See `SETUP_GUIDE.md` for detailed step-by-step instructions.

---

## Knowledge Base

Artoo uses a pre-built static Knowledge Base instead of RAG/embeddings:

| File | Description |
|------|-------------|
| `code_intelligence/data/file_summaries.json` | Per-file summaries, exports, imports, key concepts |
| `code_intelligence/data/repo_map.txt` | Directory tree with function/class names |
| `code_intelligence/data/co_change.json` | File pairs that change together in git history |
| `code_intelligence/data/scope_baselines.json` | Average files changed per ticket type |
| `code_intelligence/data/module_keywords.json` | Domain keyword to module mapping |

To build the KB for a new repo:
```bash
python helper_scripts/setup_knowledge_base.py
```

---

## Project Structure

```
agents/           16 specialized agents + supervisor orchestration
schemas/          Pydantic v2 data models for all pipeline stages
prompts/          LLM prompt templates (system + human pairs)
config/           Settings (Pydantic BaseSettings) + logging
mcp_client/       MCP server configs for JIRA, Confluence, GitHub
llm/              LLM provider factory (Gemini, Bedrock, OpenAI)
persistence/      SQLAlchemy ORM + SQLite
code_intelligence/ Static Knowledge Base builder + runtime loader
utils/            File index, markdown parser, retry, sanitizer
scheduler/        JIRA poller + PR reconciler
metrics/          KPI computation + FastAPI server
helper_scripts/   KB builders, batch runners, comparison tools
dashboard.py      Streamlit UI
main.py           CLI entry point
```

See `COMPLETE_ARCHITECTURE.md` for the full technical documentation.

---

## Requirements

- Python 3.11+
- Node.js 18+ (for GitHub MCP server)
- API credentials: one of Gemini/OpenAI/Bedrock + JIRA + GitHub

---

## License

Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
Proprietary and confidential. See LICENSE file for details.
