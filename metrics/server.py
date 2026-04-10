# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from config.settings import settings
from metrics.poc_metrics import POCMetrics, POCMetricsCollector
from persistence.database import init_db
from persistence.repository import TicketRepository

app = FastAPI(title="Artoo — POC Metrics", version="1.0.0")
_collector = POCMetricsCollector()
_repo = TicketRepository()


def _require_api_key(x_api_key: str = Header(..., alias="X-Api-Key")) -> None:
    """Verify the X-Api-Key header matches METRICS_API_KEY from settings."""
    if not settings.metrics_api_key or x_api_key != settings.metrics_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/metrics", response_model=None)
def get_metrics():
    """Return current POC KPI metrics as JSON."""
    m: POCMetrics = _collector.compute()
    return JSONResponse(
        content={
            "computed_at": m.computed_at,
            "kpi_summary": {
                "kpi1_pr_approval_rate": {
                    "target": "≥ 33%",
                    "current": f"{m.pr_approval_rate:.1%}",
                    "met": m.kpi1_met,
                    "total_prs_created": m.total_prs_created,
                    "total_prs_approved": m.total_prs_approved,
                    "total_prs_resolved": m.total_prs_resolved,
                },
                "kpi2_incomplete_detection_rate": {
                    "target": "≥ 50%",
                    "current": f"{m.incomplete_detection_rate:.1%}",
                    "met": m.kpi2_met,
                    "total_tickets_processed": m.total_tickets_processed,
                    "total_detected_incomplete": m.total_detected_incomplete,
                    "total_ground_truth_incomplete": m.total_ground_truth_incomplete,
                    "true_positive_detections": m.true_positive_detections,
                    "note": m.kpi2_note,
                },
                "kpi3_error_free_runs": {
                    "target": "≥ 10 consecutive",
                    "current": m.consecutive_error_free_runs,
                    "met": m.kpi3_met,
                    "total_runs": m.total_runs,
                    "total_error_runs": m.total_error_runs,
                },
            },
            "performance": {
                "average_duration_seconds": m.average_duration_seconds,
                "average_tokens_per_run": m.average_tokens_per_run,
                "runs_complete_pipeline": m.runs_complete_pipeline,
                "runs_flagged_incomplete": m.runs_flagged_incomplete,
            },
        }
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/pr/{run_id}/approve", dependencies=[Depends(_require_api_key)])
def mark_pr_approved(run_id: str):
    """Manually mark a PR run as approved (for KPI 1 tracking)."""
    from persistence.models import PROutcome
    _repo.set_pr_outcome(run_id, PROutcome.APPROVED)
    return {"run_id": run_id, "outcome": "approved"}


@app.post("/pr/{run_id}/reject", dependencies=[Depends(_require_api_key)])
def mark_pr_rejected(run_id: str):
    """Manually mark a PR run as rejected."""
    from persistence.models import PROutcome
    _repo.set_pr_outcome(run_id, PROutcome.REJECTED)
    return {"run_id": run_id, "outcome": "rejected"}


@app.post("/ticket/{ticket_id}/incomplete", dependencies=[Depends(_require_api_key)])
def mark_ticket_truly_incomplete(ticket_id: str):
    """Manually mark a ticket as truly incomplete for KPI 2 tracking."""
    _repo.set_ground_truth(ticket_id, truly_incomplete=True, labeled_by="Dashboard")
    return {"ticket_id": ticket_id, "truly_incomplete": True}


@app.post("/ticket/{ticket_id}/complete", dependencies=[Depends(_require_api_key)])
def mark_ticket_actually_complete(ticket_id: str):
    """Manually mark a ticket as actually complete for KPI 2 tracking."""
    _repo.set_ground_truth(ticket_id, truly_incomplete=False, labeled_by="Dashboard")
    return {"ticket_id": ticket_id, "truly_incomplete": False}


if __name__ == "__main__":
    uvicorn.run(
        "metrics.server:app",
        host="0.0.0.0",
        port=settings.metrics_port,
        reload=False,
    )
