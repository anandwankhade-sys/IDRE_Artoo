# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from persistence.database import get_db_session
from persistence.models import PROutcome, RunStatus, TicketGroundTruth, TicketRun


@dataclass
class POCMetrics:
    """
    The 3 POC success criteria plus supporting metrics.
    """

    # ── KPI 1: PR Approval Rate ≥ 33% ────────────────────────────────────────
    total_prs_created: int = 0
    total_prs_resolved: int = 0   # Approved + rejected + merged (not PENDING)
    total_prs_approved: int = 0   # APPROVED + MERGED
    pr_approval_rate: float = 0.0
    kpi1_met: bool = False

    # ── KPI 2: Incomplete Ticket Detection ≥ 50% ────────────────────────────
    total_tickets_processed: int = 0
    total_detected_incomplete: int = 0    # System flagged as incomplete
    total_ground_truth_incomplete: int = 0  # Manually labeled as truly incomplete
    true_positive_detections: int = 0     # System flagged AND truly incomplete
    incomplete_detection_rate: float = 0.0
    kpi2_met: bool = False
    kpi2_note: str = ""

    # ── KPI 3: 10 consecutive error-free runs ────────────────────────────────
    total_runs: int = 0
    total_error_runs: int = 0
    consecutive_error_free_runs: int = 0
    kpi3_met: bool = False

    # ── Supporting metrics ────────────────────────────────────────────────────
    average_duration_seconds: Optional[float] = None
    average_tokens_per_run: Optional[float] = None
    runs_complete_pipeline: int = 0   # Tickets that reached PR creation
    runs_flagged_incomplete: int = 0  # Tickets stopped at completeness gate
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class POCMetricsCollector:
    """
    Reads SQLite TicketRun records to compute all 3 POC KPIs.

    KPI 1 — PR Approval Rate:
        Tracks TicketRun.pr_outcome; updated hourly by the PR reconciler.

    KPI 2 — Incomplete Ticket Detection:
        System detection: TicketRun.ticket_deemed_incomplete = True
        Ground truth: TicketGroundTruth table (populated via CLI label tool)
        Rate = true positives / total ground-truth incomplete tickets

    KPI 3 — Error-Free Runs:
        Counts trailing consecutive runs with no errors.
    """

    def compute(self) -> POCMetrics:
        m = POCMetrics()
        with get_db_session() as session:
            self._compute_kpi1(session, m)
            self._compute_kpi2(session, m)
            self._compute_kpi3(session, m)
            self._compute_support(session, m)
        return m

    def _compute_kpi1(self, session, m: POCMetrics) -> None:
        m.total_prs_created = session.execute(
            select(func.count(TicketRun.id)).where(TicketRun.pr_url.isnot(None))
        ).scalar() or 0

        m.total_prs_approved = session.execute(
            select(func.count(TicketRun.id)).where(
                TicketRun.pr_outcome.in_([PROutcome.APPROVED, PROutcome.MERGED])
            )
        ).scalar() or 0

        m.total_prs_resolved = session.execute(
            select(func.count(TicketRun.id)).where(
                TicketRun.pr_outcome.in_([PROutcome.APPROVED, PROutcome.MERGED, PROutcome.REJECTED])
            )
        ).scalar() or 0

        m.pr_approval_rate = (
            m.total_prs_approved / m.total_prs_resolved
            if m.total_prs_resolved > 0
            else 0.0
        )
        m.kpi1_met = m.pr_approval_rate >= 0.33

    def _compute_kpi2(self, session, m: POCMetrics) -> None:
        m.total_tickets_processed = (
            session.execute(select(func.count(TicketRun.id))).scalar() or 0
        )

        m.total_detected_incomplete = session.execute(
            select(func.count(TicketRun.id)).where(
                TicketRun.ticket_deemed_incomplete == True
            )
        ).scalar() or 0

        # Ground truth: how many are labeled as truly incomplete?
        m.total_ground_truth_incomplete = session.execute(
            select(func.count(TicketGroundTruth.ticket_id)).where(
                TicketGroundTruth.truly_incomplete == True
            )
        ).scalar() or 0

        if m.total_ground_truth_incomplete == 0:
            # No ground truth labels yet — use total detected as proxy
            m.incomplete_detection_rate = 0.0
            m.kpi2_note = (
                "No ground-truth labels found. "
                "Use 'python -m metrics.label_ticket TICKET-ID --truly-incomplete' to add labels."
            )
        else:
            # True positives: detected as incomplete AND labeled as truly incomplete
            # Query by joining on ticket_id
            tp_query = """
                SELECT COUNT(DISTINCT tr.ticket_id)
                FROM ticket_runs tr
                JOIN ticket_ground_truth gt ON tr.ticket_id = gt.ticket_id
                WHERE tr.ticket_deemed_incomplete = 1
                  AND gt.truly_incomplete = 1
            """
            from sqlalchemy import text
            m.true_positive_detections = (
                session.execute(text(tp_query)).scalar() or 0
            )
            m.incomplete_detection_rate = (
                m.true_positive_detections / m.total_ground_truth_incomplete
            )
            m.kpi2_note = ""

        m.kpi2_met = m.incomplete_detection_rate >= 0.50

    def _compute_kpi3(self, session, m: POCMetrics) -> None:
        runs = session.execute(
            select(TicketRun.error_occurred, TicketRun.status)
            .order_by(TicketRun.started_at.asc())
        ).all()

        m.total_runs = len(runs)
        m.total_error_runs = sum(
            1 for r in runs
            if r.error_occurred or r.status == RunStatus.FAILED
        )

        # Count trailing consecutive error-free runs
        consecutive = 0
        for run in reversed(runs):
            if not run.error_occurred and run.status != RunStatus.FAILED:
                consecutive += 1
            else:
                break

        m.consecutive_error_free_runs = consecutive
        m.kpi3_met = consecutive >= 10

    def _compute_support(self, session, m: POCMetrics) -> None:
        avg_dur = session.execute(
            select(func.avg(TicketRun.total_duration_seconds)).where(
                TicketRun.total_duration_seconds.isnot(None)
            )
        ).scalar()

        avg_tok = session.execute(
            select(func.avg(TicketRun.total_tokens_used)).where(
                TicketRun.total_tokens_used > 0
            )
        ).scalar()

        m.average_duration_seconds = round(float(avg_dur), 2) if avg_dur else None
        m.average_tokens_per_run = round(float(avg_tok), 0) if avg_tok else None

        m.runs_complete_pipeline = session.execute(
            select(func.count(TicketRun.id)).where(
                TicketRun.status == RunStatus.COMPLETED_COMPLETE
            )
        ).scalar() or 0

        m.runs_flagged_incomplete = session.execute(
            select(func.count(TicketRun.id)).where(
                TicketRun.status == RunStatus.COMPLETED_INCOMPLETE
            )
        ).scalar() or 0
