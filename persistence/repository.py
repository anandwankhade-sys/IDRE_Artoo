# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from persistence.database import get_db_session
from persistence.models import (
    LLMCallLog,
    PROutcome,
    ProcessedTicket,
    RunStatus,
    TicketGroundTruth,
    TicketRun,
)


class TicketRepository:
    """CRUD operations for ticket processing records."""

    def is_ticket_processed(self, ticket_id: str) -> bool:
        """Return True if the ticket has already been queued/processed."""
        with get_db_session() as session:
            row = session.get(ProcessedTicket, ticket_id)
            if row is None:
                return False
            return not row.reprocess_requested

    def mark_ticket_queued(self, ticket_id: str, run_id: str) -> None:
        with get_db_session() as session:
            existing = session.get(ProcessedTicket, ticket_id)
            if existing:
                existing.last_run_id = run_id
                existing.reprocess_requested = False
            else:
                session.add(
                    ProcessedTicket(
                        ticket_id=ticket_id,
                        first_seen_at=datetime.utcnow(),
                        last_run_id=run_id,
                    )
                )

    def create_run(self, run_id: str, ticket_id: str) -> None:
        with get_db_session() as session:
            session.add(
                TicketRun(
                    id=run_id,
                    ticket_id=ticket_id,
                    status=RunStatus.RUNNING,
                    started_at=datetime.utcnow(),
                )
            )

    def update_run(self, run_id: str, **kwargs) -> None:
        with get_db_session() as session:
            run = session.get(TicketRun, run_id)
            if run:
                for k, v in kwargs.items():
                    setattr(run, k, v)

    def finalize_run(self, run_id: str, state: dict) -> None:
        """Persist final workflow state metrics to TicketRun row."""
        from schemas.workflow_state import WorkflowPhase

        phase = state.get("current_phase", WorkflowPhase.COMPLETED)
        errors = state.get("errors", [])
        error_occurred = bool(errors)

        completeness = state.get("completeness_result")
        pr_result = state.get("pr_result")

        started_at_str = state.get("started_at")
        started_dt = (
            datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            if started_at_str
            else datetime.utcnow()
        )
        duration = (datetime.utcnow() - started_dt.replace(tzinfo=None)).total_seconds()

        status = (
            RunStatus.FAILED
            if error_occurred
            else (
                RunStatus.COMPLETED_INCOMPLETE
                if state.get("is_complete_ticket") is False
                else RunStatus.COMPLETED_COMPLETE
            )
        )

        pr_outcome = PROutcome.NOT_CREATED
        pr_url = None
        pr_number = None
        pr_branch = None
        if pr_result:
            pr_url = pr_result.pr_url
            pr_number = pr_result.pr_number
            pr_branch = pr_result.branch_name
            pr_outcome = PROutcome.PENDING if pr_result.pr_url else PROutcome.NOT_CREATED

        # Sum token counts directly from LLMCallLog rows — workflow state never
        # accumulates total_tokens_used so state.get() always returns 0.
        total_tokens = 0
        try:
            with get_db_session() as tok_session:
                result = tok_session.execute(
                    select(func.sum(LLMCallLog.total_token_count)).where(
                        LLMCallLog.run_id == run_id
                    )
                )
                total_tokens = result.scalar() or 0
        except Exception:
            total_tokens = state.get("total_tokens_used", 0)

        updates = dict(
            status=status,
            completed_at=datetime.utcnow(),
            completeness_score=completeness.completeness_score if completeness else None,
            ticket_deemed_incomplete=(
                completeness.decision.value == "incomplete" if completeness else None
            ),
            clarification_comment_posted=(
                completeness.jira_comment_posted if completeness else False
            ),
            implementation_plan_generated=state.get("implementation_plan") is not None,
            code_proposal_generated=state.get("code_proposal") is not None,
            tests_suggested=state.get("test_suggestions") is not None,
            pr_url=pr_url,
            pr_number=pr_number,
            pr_branch=pr_branch,
            pr_outcome=pr_outcome,
            total_duration_seconds=duration,
            total_llm_calls=state.get("total_llm_calls", 0),
            total_tokens_used=total_tokens,
            error_occurred=error_occurred,
            error_phase=str(phase) if error_occurred else None,
            error_message="; ".join(errors) if errors else None,
            final_state_snapshot=_serialise_state(state),
        )
        self.update_run(run_id, **updates)

    def save_llm_call(self, record) -> None:
        """Persist an LLMCallRecord to the DB."""
        with get_db_session() as session:
            session.add(
                LLMCallLog(
                    id=record.call_id,
                    run_id=record.run_id,
                    ticket_id=record.ticket_id,
                    agent_name=record.agent_name,
                    model_id=record.model_id,
                    prompt_template_name=record.prompt_template_name,
                    prompt_token_count=record.prompt_token_count,
                    parsed_successfully=record.parsed_successfully,
                    completion_token_count=record.completion_token_count,
                    total_token_count=record.total_token_count,
                    latency_ms=record.latency_ms,
                    invoked_at=datetime.fromisoformat(record.invoked_at),
                    error_occurred=record.error_occurred,
                    error_type=record.error_type,
                    error_message=record.error_message,
                )
            )

    def request_reprocess(self, ticket_id: str) -> None:
        with get_db_session() as session:
            row = session.get(ProcessedTicket, ticket_id)
            if row:
                row.reprocess_requested = True

    def set_pr_outcome(self, run_id: str, outcome: PROutcome) -> None:
        self.update_run(run_id, pr_outcome=outcome)

    def get_pending_pr_runs(self) -> list[TicketRun]:
        with get_db_session() as session:
            rows = session.execute(
                select(TicketRun).where(TicketRun.pr_outcome == PROutcome.PENDING)
            ).scalars().all()
            # Detach from session before returning
            session.expunge_all()
            return rows

    def set_ground_truth(
        self,
        ticket_id: str,
        truly_incomplete: bool,
        labeled_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        with get_db_session() as session:
            existing = session.get(TicketGroundTruth, ticket_id)
            if existing:
                existing.truly_incomplete = truly_incomplete
                existing.labeled_by = labeled_by
                existing.labeled_at = datetime.utcnow()
                existing.notes = notes
            else:
                session.add(
                    TicketGroundTruth(
                        ticket_id=ticket_id,
                        truly_incomplete=truly_incomplete,
                        labeled_by=labeled_by,
                        notes=notes,
                    )
                )


def _serialise_state(state: dict) -> dict:
    """Convert WorkflowState to a JSON-serialisable dict for snapshot storage."""
    result = {}
    for k, v in state.items():
        if hasattr(v, "model_dump"):
            result[k] = v.model_dump(mode="json")
        elif isinstance(v, list):
            result[k] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in v
            ]
        else:
            result[k] = v
    return result
