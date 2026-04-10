# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED_COMPLETE = "completed_complete"      # Full pipeline ran
    COMPLETED_INCOMPLETE = "completed_incomplete"  # Stopped at clarification gate
    FAILED = "failed"


class PROutcome(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    NOT_CREATED = "not_created"


class TicketRun(Base):
    """One record per ticket processing attempt. Primary table for KPI computation."""

    __tablename__ = "ticket_runs"

    id = Column(String(36), primary_key=True)       # run_id (UUID)
    ticket_id = Column(String(50), nullable=False, index=True)

    # Status
    status = Column(SAEnum(RunStatus), nullable=False, default=RunStatus.RUNNING)
    current_phase = Column(String(50), nullable=True, default="fetching_ticket")
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Completeness gate
    completeness_score = Column(Float, nullable=True)
    ticket_deemed_incomplete = Column(Boolean, nullable=True)
    clarification_comment_posted = Column(Boolean, default=False)

    # Pipeline results (null when ticket was incomplete)
    implementation_plan_generated = Column(Boolean, default=False)
    code_proposal_generated = Column(Boolean, default=False)
    tests_suggested = Column(Boolean, default=False)

    # PR outcome
    pr_url = Column(String(500), nullable=True)
    pr_number = Column(Integer, nullable=True)
    pr_branch = Column(String(200), nullable=True)
    pr_outcome = Column(SAEnum(PROutcome), default=PROutcome.NOT_CREATED)

    # Performance
    total_duration_seconds = Column(Float, nullable=True)
    total_llm_calls = Column(Integer, default=0)
    total_tokens_used = Column(Integer, default=0)

    # Error tracking
    error_occurred = Column(Boolean, default=False)
    error_phase = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)

    # Full state snapshot for debugging
    final_state_snapshot = Column(JSON, nullable=True)

    llm_calls = relationship("LLMCallLog", back_populates="ticket_run")


class LLMCallLog(Base):
    """One record per LLM invocation. FK to TicketRun."""

    __tablename__ = "llm_call_logs"

    id = Column(String(36), primary_key=True)       # call_id (UUID)
    run_id = Column(String(36), ForeignKey("ticket_runs.id"), nullable=False)
    ticket_id = Column(String(50), nullable=False)
    agent_name = Column(String(100), nullable=False)

    # Request
    model_id = Column(String(100), nullable=False)
    prompt_template_name = Column(String(200), nullable=False)
    prompt_token_count = Column(Integer, nullable=True)

    # Response
    parsed_successfully = Column(Boolean, nullable=False)
    completion_token_count = Column(Integer, nullable=True)
    total_token_count = Column(Integer, nullable=True)

    # Performance
    latency_ms = Column(Float, nullable=False)
    invoked_at = Column(DateTime, nullable=False)

    # Error
    error_occurred = Column(Boolean, default=False)
    error_type = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)

    ticket_run = relationship("TicketRun", back_populates="llm_calls")


class ProcessedTicket(Base):
    """Deduplication table — prevents re-processing same ticket on subsequent polls."""

    __tablename__ = "processed_tickets"

    ticket_id = Column(String(50), primary_key=True)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_run_id = Column(String(36), nullable=True)
    reprocess_requested = Column(Boolean, default=False)


class TicketGroundTruth(Base):
    """
    Manual ground-truth labels for KPI 2 (incomplete ticket detection rate).
    Populated via: python -m metrics.label_ticket PROJ-123 --truly-incomplete
    """

    __tablename__ = "ticket_ground_truth"

    ticket_id = Column(String(50), primary_key=True)
    truly_incomplete = Column(Boolean, nullable=False)
    labeled_by = Column(String(100), nullable=True)
    labeled_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    notes = Column(Text, nullable=True)
