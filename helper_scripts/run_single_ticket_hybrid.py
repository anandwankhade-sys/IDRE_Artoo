import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone

# Add current dir to sys.path
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# To get all functions and constants
from multi_model_benchmark import *
from multi_model_benchmark import _log  # Explicitly import underscored function

def run_single(ticket_id: str, model_slug: str):
    _log(f"\n[SINGLE RUN] Ticket: {ticket_id} | Model: {model_slug}")
    
    # 1. Fetch JIRA
    raw_jira = fetch_jira(ticket_id)
    if not raw_jira:
        _log("  ERROR: Could not fetch JIRA data.")
        return
    
    jira_data = {ticket_id: raw_jira}

    # 2. Fetch Confluence
    import multi_model_benchmark
    multi_model_benchmark.TARGET_JIRA_IDS = [ticket_id]
    multi_model_benchmark.MAX_PARALLEL_TICKETS = 1
    
    conf_data = fetch_all_confluence(jira_data)

    # 3. Load KT Summary
    kt_sections = load_kt_sections()

    # 4. Get Model Config
    model_config = next((m for m in MODELS if m["slug"] == model_slug), MODELS[-1])

    # 5. Load Retriever (optional, but good for codebase grounding)
    _log("[RAG] Initialising retriever...")
    try:
        from rag_retriever_improved import get_improved_retriever
        retriever = get_improved_retriever()
    except Exception as e:
        _log(f"  WARN: No retriever: {e}")
        retriever = None

    # 6. Run Pipeline
    results = run_model_batch(
        model_config=model_config,
        jira_data=jira_data,
        conf_data=conf_data,
        retriever=retriever,
        kt_sections=kt_sections,
    )

    # 7. Save result
    out_dir = Path("fresh_run")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{ticket_id}_{model_slug}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    _log(f"\n[DONE] Result saved to {out_path}")

if __name__ == "__main__":
    import sys
    ticket = sys.argv[1] if len(sys.argv) > 1 else "PROJ-123"
    run_single(ticket, "gemini-2.5-pro")
