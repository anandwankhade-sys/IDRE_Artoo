# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
Artoo — Entry Point

Usage:
    # Run as scheduler (polls Jira on a schedule)
    python main.py --mode scheduler

    # Run against a single ticket manually
    python main.py --mode single --ticket PROJ-123

    # Dry-run (no Jira comments, no GitHub PRs)
    python main.py --mode single --ticket PROJ-123 --dry-run

    # Show current POC metrics
    python main.py --mode metrics

    # Start the metrics HTTP server
    python main.py --mode metrics-server
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _configure() -> None:
    from config.logging_config import configure_logging
    configure_logging()
    from persistence.database import init_db
    init_db()


def validate_settings(mode: str) -> None:
    """
    Check that required environment variables are present for the given run mode.
    Prints all missing settings and exits with code 1 if any are absent.
    """
    from config.settings import settings

    errors: list[str] = []

    if mode in ("scheduler", "single"):
        if not settings.jira_url:
            errors.append("JIRA_URL is not set")
        if not settings.jira_api_token.get_secret_value():
            errors.append("JIRA_API_TOKEN is not set")
        if not settings.jira_username:
            errors.append("JIRA_USERNAME is not set")
        if not settings.github_personal_access_token.get_secret_value():
            errors.append("GITHUB_PERSONAL_ACCESS_TOKEN is not set")
        if not settings.github_repo_owner:
            errors.append("GITHUB_REPO_OWNER is not set")
        if not settings.github_repo_name:
            errors.append("GITHUB_REPO_NAME is not set")
        if not settings.aws_profile and not (
            settings.aws_access_key_id
            and settings.aws_secret_access_key.get_secret_value()
        ):
            errors.append(
                "AWS credentials not configured: set AWS_PROFILE or "
                "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY"
            )

    if errors:
        for e in errors:
            print(f"[CONFIG ERROR] {e}", file=sys.stderr)
        sys.exit(1)


def run_scheduler() -> None:
    validate_settings("scheduler")
    _configure()
    from apscheduler.triggers.interval import IntervalTrigger
    from config.settings import settings
    from app_logging.activity_logger import ActivityLogger
    from scheduler.poller import start_scheduler, stop_scheduler
    from scheduler.pr_reconciler import reconcile_pr_outcomes

    logger = ActivityLogger("main")

    sched = start_scheduler()

    # Add PR reconciler job
    sched.add_job(
        reconcile_pr_outcomes,
        trigger=IntervalTrigger(seconds=settings.pr_reconcile_interval_seconds),
        id="pr_reconcile",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    logger.info("main_scheduler_running", pid=os.getpid())
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_scheduler()
        logger.info("main_scheduler_stopped")


def run_single(ticket_id: str, dry_run: bool) -> None:
    if dry_run:
        os.environ["DRY_RUN"] = "true"
    validate_settings("single")
    _configure()

    from agents.supervisor import run_workflow
    from app_logging.activity_logger import ActivityLogger

    logger = ActivityLogger("main")
    logger.info("single_run_started", ticket_id=ticket_id, dry_run=dry_run)

    final_state = run_workflow(ticket_id)

    # Pretty-print summary
    phase = final_state.get("current_phase", "unknown")
    errors = final_state.get("errors", [])
    pr = final_state.get("pr_result")

    lines = [
        "",
        "=" * 60,
        f"  Ticket: {ticket_id}",
        f"  Phase:  {phase}",
    ]
    if errors:
        lines.append(f"  Errors: {'; '.join(errors)}")
    if pr and hasattr(pr, "pr_url") and pr.pr_url:
        lines.append(f"  PR:     {pr.pr_url}")
    elif pr and hasattr(pr, "status"):
        lines.append(f"  PR:     {pr.status.value}")
    completeness = final_state.get("completeness_result")
    if completeness:
        lines.append(f"  Completeness: {completeness.completeness_score:.0%} ({completeness.decision.value})")
    lines.append("=" * 60)
    lines.append("")
    sys.stdout.buffer.write(("\n".join(lines)).encode("utf-8", errors="replace") + b"\n")
    sys.stdout.buffer.flush()


def show_metrics() -> None:
    _configure()
    from metrics.poc_metrics import POCMetricsCollector

    m = POCMetricsCollector().compute()

    kpi1_icon = "[PASS]" if m.kpi1_met else "[FAIL]"
    kpi2_icon = "[PASS]" if m.kpi2_met else "[FAIL]"
    kpi3_icon = "[PASS]" if m.kpi3_met else "[FAIL]"

    lines = [
        "",
        "=" * 60,
        "  POC SUCCESS CRITERIA",
        "=" * 60,
        f"  {kpi1_icon} KPI 1 - PR Approval Rate:        {m.pr_approval_rate:.1%}  (target >=33%, {m.total_prs_approved}/{m.total_prs_resolved} resolved)",
        f"  {kpi2_icon} KPI 2 - Incomplete Detection:     {m.incomplete_detection_rate:.1%}  (target >=50%)",
    ]
    if m.kpi2_note:
        lines.append(f"           Note: {m.kpi2_note}")
    lines.append(f"  {kpi3_icon} KPI 3 - Consecutive Error-Free:   {m.consecutive_error_free_runs}   (target >=10, total runs: {m.total_runs})")
    lines.append("")
    if m.average_duration_seconds is not None:
        lines.append(f"  Average processing time: {m.average_duration_seconds:.1f}s")
    if m.average_tokens_per_run is not None:
        lines.append(f"  Average tokens/run:      {m.average_tokens_per_run:.0f}")
    lines.append(f"  Complete pipeline runs:  {m.runs_complete_pipeline}")
    lines.append(f"  Flagged incomplete:      {m.runs_flagged_incomplete}")
    lines.append("=" * 60)
    lines.append("")
    sys.stdout.buffer.write(("\n".join(lines)).encode("utf-8", errors="replace") + b"\n")
    sys.stdout.buffer.flush()


def start_metrics_server() -> None:
    import uvicorn
    from config.settings import settings
    _configure()
    uvicorn.run("metrics.server:app", host="0.0.0.0", port=settings.metrics_port, reload=False)


def run_demo_launch() -> None:
    """Launch Metrics Server, Streamlit UI, and Log Viewer in separate processes."""
    import subprocess
    from config.settings import settings

    print("--- [DEMO LAUNCH] Starting Artoo services ---")

    # 1. Metrics Server
    print("-> Starting Metrics API (Port 8080)...")
    subprocess.Popen([sys.executable, "main.py", "--mode", "metrics-server"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    # 2. Streamlit Dashboard
    print("-> Starting Streamlit Dashboard (Port 8502)...")
    subprocess.Popen(["streamlit", "run", "dashboard.py", "--server.port", "8502"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    # 3. Log Viewer (PowerShell)
    print("-> Opening Log Viewer in new terminal...")
    log_cmd = (
        'Get-Content logs\\activity.jsonl -Wait -Tail 10 | ForEach-Object { '
        'try { $l = $_ | ConvertFrom-Json; '
        'Write-Host ("`n--- [{0}] [{1}] [{2}] ---" -f $l.timestamp, $l.level, $l.event) -ForegroundColor Cyan; '
        '$l.psobject.Properties | ForEach-Object { '
        'if ($_.Name -notin "timestamp", "level", "event") { '
        'Write-Host ("  {0}: {1}" -f $_.Name.PadRight(20), $_.Value) '
        '} } } catch { $_ } }'
    )
    ps_arg = f'Start-Process powershell -ArgumentList "-NoExit", "-Command", "{log_cmd}"'
    subprocess.run(["powershell", "-Command", ps_arg])

    print("\n[OK] All services launched.")
    print("Dashboard: http://localhost:8502")
    print("Metrics:   http://localhost:8080/docs")
    print("Logs:      Running in the new PowerShell window.")
    print("\nPress Ctrl+C in THIS terminal if you want to exit THIS script (background services will remain).")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDemo launcher stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Artoo")
    parser.add_argument(
        "--mode",
        choices=["scheduler", "single", "metrics", "metrics-server", "demo"],
        default="scheduler",
        help="Run mode",
    )
    parser.add_argument("--ticket", help="Jira ticket ID (required for --mode single)")
    parser.add_argument("--dry-run", action="store_true", help="Skip Jira comments and GitHub PR creation")

    args = parser.parse_args()

    if args.mode == "scheduler":
        run_scheduler()
    elif args.mode == "single":
        if not args.ticket:
            print("ERROR: --ticket is required with --mode single", file=sys.stderr)
            sys.exit(1)
        run_single(args.ticket, args.dry_run)
    elif args.mode == "metrics":
        show_metrics()
    elif args.mode == "metrics-server":
        start_metrics_server()
    elif args.mode == "demo":
        run_demo_launch()


if __name__ == "__main__":
    main()
