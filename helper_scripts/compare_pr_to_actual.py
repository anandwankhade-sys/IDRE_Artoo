# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
compare_pr_to_actual.py — Compare Artoo pipeline output to actual developer work
=================================================================================

For each processed JIRA ticket, this script:
1. Fetches the Artoo-generated code proposal (files changed, plan) from the DB
2. Fetches what the developer actually did:
   a. JIRA comments/history for resolution details
   b. Linked PRs / commits from the production GitHub repo
   c. Actual files changed in the real PR
3. Compares them and produces a report:
   - File overlap (did Artoo touch the same files?)
   - Approach similarity (did Artoo's plan match the actual fix?)
   - Accuracy score per ticket

Output: Excel report + console summary
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logging_config import configure_logging
from config.settings import settings
from persistence.database import get_db_session, init_db
from persistence.models import TicketRun
from sqlalchemy import select


def _load_artoo_results() -> list[dict]:
    """Load all completed pipeline runs with their state snapshots."""
    results = []
    with get_db_session() as session:
        rows = session.execute(
            select(TicketRun).where(
                TicketRun.status.in_(["completed_complete", "completed_incomplete"])
            ).order_by(TicketRun.started_at.desc())
        ).scalars().all()

        for r in rows:
            snapshot = r.final_state_snapshot or {}
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)

            # Extract code proposal files
            code_proposal = snapshot.get("code_proposal", {}) or {}
            file_changes = code_proposal.get("file_changes", []) if isinstance(code_proposal, dict) else []
            artoo_files = [fc.get("file_path", "") for fc in file_changes if isinstance(fc, dict)]

            # Extract plan
            plan = snapshot.get("implementation_plan", {}) or {}
            plan_steps = []
            if isinstance(plan, dict):
                for step in plan.get("steps", []):
                    if isinstance(step, dict):
                        plan_steps.append(step.get("description", ""))
                    elif isinstance(step, str):
                        plan_steps.append(step)

            plan_summary = plan.get("summary", "") if isinstance(plan, dict) else ""

            results.append({
                "ticket_id": r.ticket_id,
                "run_id": r.id,
                "status": r.status.value if r.status else "",
                "pr_url": r.pr_url or "",
                "completeness_score": r.completeness_score,
                "artoo_files": artoo_files,
                "artoo_plan_summary": plan_summary,
                "artoo_plan_steps": plan_steps,
                "artoo_code_summary": code_proposal.get("summary", "") if isinstance(code_proposal, dict) else "",
                "total_tokens": r.total_tokens_used or 0,
                "duration_s": r.total_duration_seconds or 0,
            })

    return results


async def _fetch_jira_history(ticket_id: str) -> dict:
    """Fetch JIRA ticket comments and changelog for resolution details."""
    from mcp_client.client_factory import get_mcp_client, filter_jira_tools
    from utils.mcp_helpers import find_tool, unwrap_tool_result

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        # Get full issue with comments
        get_issue = next((t for t in jira_tools if t.name == "jira_get_issue"), None)
        if not get_issue:
            return {"comments": [], "description": ""}

        result = await get_issue.ainvoke({"issue_key": ticket_id})
        data = unwrap_tool_result(result)
        fields = data.get("fields", data)

        description = fields.get("description", "") or ""
        if isinstance(description, dict):
            description = str(description)

        comments_raw = fields.get("comment", {})
        comments = []
        if isinstance(comments_raw, dict):
            for c in comments_raw.get("comments", []):
                body = c.get("body", "")
                if isinstance(body, dict):
                    body = str(body)
                comments.append({
                    "author": c.get("author", {}).get("displayName", "unknown"),
                    "body": body[:500],
                    "created": c.get("created", ""),
                })
        elif isinstance(comments_raw, list):
            for c in comments_raw:
                body = c.get("body", "") if isinstance(c, dict) else str(c)
                comments.append({"author": "unknown", "body": str(body)[:500], "created": ""})

        return {
            "description": description[:2000],
            "comments": comments,
        }


async def _fetch_dev_pr_files(ticket_id: str) -> dict:
    """Fetch actual developer PRs/commits for this ticket using multiple strategies:
    1. JIRA development info API (direct link to PRs/commits)
    2. GitHub search_issues for PRs mentioning the ticket
    3. GitHub search_code as fallback
    """
    from mcp_client.client_factory import get_mcp_client, filter_jira_tools, filter_github_tools
    from utils.mcp_helpers import find_tool, unwrap_tool_result

    owner = settings.github_repo_owner
    repo = settings.github_repo_name

    async with get_mcp_client() as client:
        all_tools = await client.get_tools()
        jira_tools = filter_jira_tools(all_tools)

        dev_files = []
        dev_pr_url = ""
        dev_pr_title = ""
        dev_pr_body = ""

        # Strategy 1: JIRA development info (links PRs/commits directly)
        dev_info_tool = next((t for t in jira_tools if t.name == "jira_get_issue_development_info"), None)
        if dev_info_tool:
            try:
                result = await dev_info_tool.ainvoke({"issue_key": ticket_id})
                data = unwrap_tool_result(result)
                if isinstance(data, str):
                    import json
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = {}

                # Extract PR info from development details
                if isinstance(data, dict):
                    # Check for pull requests
                    prs = data.get("pullRequests", data.get("pull_requests", []))
                    if isinstance(prs, list) and prs:
                        pr = prs[0] if isinstance(prs[0], dict) else {}
                        dev_pr_url = pr.get("url", pr.get("html_url", ""))
                        dev_pr_title = pr.get("title", pr.get("name", ""))

                    # Check for commits
                    commits = data.get("commits", [])
                    if isinstance(commits, list):
                        for c in commits:
                            if isinstance(c, dict):
                                files = c.get("files", [])
                                if isinstance(files, list):
                                    dev_files.extend(
                                        f.get("path", f.get("filename", ""))
                                        for f in files if isinstance(f, dict)
                                    )

                    # Check for branches (may contain PR references)
                    branches = data.get("branches", [])
                    if isinstance(branches, list):
                        for b in branches:
                            if isinstance(b, dict):
                                name = b.get("name", "")
                                if name and not dev_pr_title:
                                    dev_pr_title = f"Branch: {name}"

                    # Try raw text parsing if structured data is empty
                    if not dev_pr_url and isinstance(data, dict):
                        raw = str(data)
                        import re
                        pr_match = re.search(r'https://github\.com/[^/]+/[^/]+/pull/\d+', raw)
                        if pr_match:
                            dev_pr_url = pr_match.group(0)
            except Exception:
                pass

        # Strategy 2: GitHub search_issues for PRs mentioning ticket
        if not dev_pr_url:
            search_issues = next((t for t in all_tools if t.name == "search_issues"), None)
            if search_issues:
                try:
                    result = await search_issues.ainvoke({
                        "q": f"{ticket_id} repo:{owner}/{repo} is:pr",
                    })
                    data = unwrap_tool_result(result)
                    items = []
                    if isinstance(data, dict):
                        items = data.get("items", data.get("issues", []))
                    elif isinstance(data, list):
                        items = data

                    for item in items[:5]:
                        if not isinstance(item, dict):
                            continue
                        title = item.get("title", "")
                        body = item.get("body", "") or ""
                        if ticket_id.lower() in title.lower() or ticket_id.lower() in body.lower():
                            pr_num = item.get("number")
                            dev_pr_url = item.get("html_url", f"https://github.com/{owner}/{repo}/pull/{pr_num}")
                            dev_pr_title = title
                            dev_pr_body = body[:1000]
                            break
                except Exception:
                    pass

        # Strategy 3: If we found a PR URL, try to get its files
        if dev_pr_url and not dev_files:
            get_pr_files_tool = next((t for t in all_tools if t.name == "get_pull_request_files"), None)
            if get_pr_files_tool:
                # Extract PR number from URL
                import re
                pr_match = re.search(r'/pull/(\d+)', dev_pr_url)
                if pr_match:
                    pr_num = int(pr_match.group(1))
                    # Determine the correct owner/repo from the URL
                    repo_match = re.search(r'github\.com/([^/]+)/([^/]+)/pull', dev_pr_url)
                    pr_owner = repo_match.group(1) if repo_match else owner
                    pr_repo = repo_match.group(2) if repo_match else repo
                    try:
                        files_result = await get_pr_files_tool.ainvoke({
                            "owner": pr_owner,
                            "repo": pr_repo,
                            "pull_number": pr_num,
                        })
                        files_data = unwrap_tool_result(files_result)
                        if isinstance(files_data, list):
                            dev_files = [f.get("filename", "") for f in files_data if isinstance(f, dict)]
                        elif isinstance(files_data, dict):
                            dev_files = [f.get("filename", "") for f in files_data.get("files", files_data.get("items", [])) if isinstance(f, dict)]
                    except Exception:
                        pass

        return {
            "dev_files": [f for f in dev_files if f],
            "dev_pr_url": dev_pr_url,
            "dev_pr_title": dev_pr_title,
            "dev_pr_body": dev_pr_body,
        }


def _compute_file_overlap(artoo_files: list[str], dev_files: list[str]) -> dict:
    """Compare file lists and return overlap metrics."""
    if not artoo_files or not dev_files:
        return {
            "artoo_count": len(artoo_files),
            "dev_count": len(dev_files),
            "exact_overlap": 0,
            "basename_overlap": 0,
            "overlap_pct": 0.0,
            "matched_files": [],
            "artoo_only": artoo_files,
            "dev_only": dev_files,
        }

    # Normalize paths
    artoo_norm = {f.replace("\\", "/").lstrip("/") for f in artoo_files}
    dev_norm = {f.replace("\\", "/").lstrip("/") for f in dev_files}

    # Exact path matches
    exact = artoo_norm & dev_norm

    # Basename matches (same filename, maybe different dir)
    artoo_basenames = {os.path.basename(f): f for f in artoo_norm}
    dev_basenames = {os.path.basename(f): f for f in dev_norm}
    basename_matches = set(artoo_basenames.keys()) & set(dev_basenames.keys())
    basename_overlap = basename_matches - {os.path.basename(f) for f in exact}

    total_overlap = len(exact) + len(basename_overlap)
    max_files = max(len(artoo_norm), len(dev_norm))
    overlap_pct = total_overlap / max_files if max_files > 0 else 0.0

    return {
        "artoo_count": len(artoo_norm),
        "dev_count": len(dev_norm),
        "exact_overlap": len(exact),
        "basename_overlap": len(basename_overlap),
        "overlap_pct": round(overlap_pct * 100, 1),
        "matched_files": sorted(exact) + [f"{artoo_basenames[b]} ~ {dev_basenames[b]}" for b in basename_overlap],
        "artoo_only": sorted(artoo_norm - dev_norm - {artoo_basenames.get(b, "") for b in basename_overlap}),
        "dev_only": sorted(dev_norm - artoo_norm - {dev_basenames.get(b, "") for b in basename_overlap}),
    }


async def analyze_ticket(artoo_result: dict) -> dict:
    """Analyze one ticket: fetch dev work and compare."""
    ticket_id = artoo_result["ticket_id"]
    print(f"  Analyzing {ticket_id}...", end=" ", flush=True)

    try:
        jira_info = await _fetch_jira_history(ticket_id)
    except Exception as e:
        jira_info = {"comments": [], "description": f"Error: {e}"}

    try:
        dev_info = await _fetch_dev_pr_files(ticket_id)
    except Exception as e:
        dev_info = {"dev_files": [], "dev_pr_url": "", "dev_pr_title": "", "dev_pr_body": ""}

    overlap = _compute_file_overlap(artoo_result["artoo_files"], dev_info["dev_files"])

    print(f"Files: Artoo={overlap['artoo_count']}, Dev={overlap['dev_count']}, "
          f"Overlap={overlap['overlap_pct']}%")

    return {
        **artoo_result,
        "dev_pr_url": dev_info["dev_pr_url"],
        "dev_pr_title": dev_info["dev_pr_title"],
        "dev_files": dev_info["dev_files"],
        "dev_file_count": len(dev_info["dev_files"]),
        "file_overlap_pct": overlap["overlap_pct"],
        "exact_file_matches": overlap["exact_overlap"],
        "basename_file_matches": overlap["basename_overlap"],
        "matched_files": overlap["matched_files"],
        "artoo_only_files": overlap["artoo_only"],
        "dev_only_files": overlap["dev_only"],
        "jira_comments_count": len(jira_info["comments"]),
        "jira_comments": jira_info["comments"],
    }


def generate_excel_report(analyses: list[dict], output_path: str):
    """Generate an Excel report comparing Artoo vs actual dev work."""
    import pandas as pd

    # Summary sheet
    summary_data = []
    for a in analyses:
        summary_data.append({
            "Ticket": a["ticket_id"],
            "Status": a["status"],
            "Completeness": f"{a['completeness_score']:.0%}" if a["completeness_score"] else "—",
            "Artoo PR": a["pr_url"],
            "Dev PR": a["dev_pr_url"],
            "Artoo Files": len(a["artoo_files"]),
            "Dev Files": a["dev_file_count"],
            "File Overlap %": a["file_overlap_pct"],
            "Exact Matches": a["exact_file_matches"],
            "Basename Matches": a["basename_file_matches"],
            "Artoo Plan": a["artoo_plan_summary"][:200],
            "Dev PR Title": a["dev_pr_title"][:200],
            "Duration (s)": f"{a['duration_s']:.0f}",
            "Tokens": a["total_tokens"],
        })

    df_summary = pd.DataFrame(summary_data)

    # File detail sheet
    file_details = []
    for a in analyses:
        for f in a.get("matched_files", []):
            file_details.append({"Ticket": a["ticket_id"], "Match Type": "Match", "File": f})
        for f in a.get("artoo_only_files", []):
            file_details.append({"Ticket": a["ticket_id"], "Match Type": "Artoo Only", "File": f})
        for f in a.get("dev_only_files", []):
            file_details.append({"Ticket": a["ticket_id"], "Match Type": "Dev Only", "File": f})

    df_files = pd.DataFrame(file_details) if file_details else pd.DataFrame(columns=["Ticket", "Match Type", "File"])

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        df_files.to_excel(writer, sheet_name="File Details", index=False)

    print(f"\nReport saved to: {output_path}")


def main():
    configure_logging()
    init_db()

    print("Loading Artoo pipeline results...")
    artoo_results = _load_artoo_results()
    print(f"Found {len(artoo_results)} completed runs")

    if not artoo_results:
        print("No completed runs to analyze.")
        return

    print("\nFetching developer work from JIRA + GitHub...")
    analyses = []
    for result in artoo_results:
        try:
            analysis = asyncio.run(analyze_ticket(result))
            analyses.append(analysis)
        except Exception as e:
            print(f"  {result['ticket_id']}: FAILED - {e}")
            analyses.append({**result, "dev_pr_url": "", "dev_pr_title": "",
                           "dev_files": [], "dev_file_count": 0, "file_overlap_pct": 0,
                           "exact_file_matches": 0, "basename_file_matches": 0,
                           "matched_files": [], "artoo_only_files": result["artoo_files"],
                           "dev_only_files": [], "jira_comments_count": 0, "jira_comments": []})

    # Print summary
    print(f"\n{'='*60}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*60}")

    complete_runs = [a for a in analyses if a["status"] == "completed_complete"]
    incomplete_runs = [a for a in analyses if a["status"] == "completed_incomplete"]

    print(f"  Complete pipeline runs: {len(complete_runs)}")
    print(f"  Incomplete (stopped at gate): {len(incomplete_runs)}")

    if complete_runs:
        with_dev_pr = [a for a in complete_runs if a["dev_pr_url"]]
        print(f"  Runs with matching dev PR found: {len(with_dev_pr)}")

        if with_dev_pr:
            avg_overlap = sum(a["file_overlap_pct"] for a in with_dev_pr) / len(with_dev_pr)
            print(f"  Average file overlap: {avg_overlap:.1f}%")

        for a in complete_runs:
            dev_info = f"Dev PR: {a['dev_pr_url']}" if a["dev_pr_url"] else "No dev PR found"
            print(f"  {a['ticket_id']:10s} | Files: {len(a['artoo_files'])}A/{a['dev_file_count']}D | "
                  f"Overlap: {a['file_overlap_pct']}% | {dev_info}")

    # Generate Excel
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        f"artoo_vs_actual_{timestamp}.xlsx"
    )
    generate_excel_report(analyses, output_path)


if __name__ == "__main__":
    main()
