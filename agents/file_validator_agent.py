# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
agents/file_validator_agent.py
================================
Post-code-proposal validation step.

Runs immediately after CodeProposalAgent and:
  1. Checks every proposed file path against the real codebase index
  2. Corrects paths that are fuzzy matches (right filename, wrong dir)
  3. Removes paths that are entirely hallucinated
  4. Logs a hallucination_rate metric for observability

No LLM call needed — purely index-based.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from app_logging.activity_logger import ActivityLogger
from schemas.code_proposal import ChangeType, CodeProposal
from schemas.workflow_state import WorkflowPhase, WorkflowState
from utils.file_index import validate_proposed_paths

logger = ActivityLogger("file_validator_agent")


class FileValidatorAgent(BaseAgent):
    def run(self, state: WorkflowState) -> dict:
        code_proposal: CodeProposal | None = state.get("code_proposal")
        ticket_id = state["ticket_id"]
        run_id = state["run_id"]

        self.logger.info(
            "agent_node_entered",
            ticket_id=ticket_id,
            run_id=run_id,
            phase="file_validation",
        )

        if code_proposal is None or not code_proposal.file_changes:
            # Nothing to validate — pass through
            return {
                "current_phase": WorkflowPhase.SUGGESTING_TESTS,
                "file_validation_result": {
                    "skipped": True,
                    "reason": "no code proposal or no file changes",
                },
            }

        # Only validate paths for files being MODIFIED — new files (create)
        # are expected to not exist and should not be flagged as hallucinated.
        new_file_paths = {
            fc.file_path.replace("\\", "/").lstrip("/")
            for fc in code_proposal.file_changes
            if fc.change_type == ChangeType.CREATE
        }
        paths_to_validate = [
            fc.file_path for fc in code_proposal.file_changes
            if fc.file_path.replace("\\", "/").lstrip("/") not in new_file_paths
        ]

        if not paths_to_validate:
            # All files are new creations — nothing to validate
            self.logger.info(
                "file_validation_all_new",
                ticket_id=ticket_id,
                run_id=run_id,
                new_files=len(new_file_paths),
            )
            return {
                "current_phase": WorkflowPhase.SUGGESTING_TESTS,
                "file_validation_result": {
                    "total_proposed": len(code_proposal.file_changes),
                    "new_files": len(new_file_paths),
                    "hallucination_rate": 0.0,
                    "hallucinated": [],
                },
            }

        validation = validate_proposed_paths(paths_to_validate)

        self.logger.info(
            "file_validation_complete",
            ticket_id=ticket_id,
            run_id=run_id,
            total=validation["total_proposed"],
            exact=len(validation["exact_matches"]),
            fuzzy=len(validation["fuzzy_matches"]),
            hallucinated=len(validation["hallucinated"]),
            hallucination_rate=round(validation["hallucination_rate"], 3),
        )

        # Apply corrections: fix fuzzy paths in-place
        corrections = validation["corrections"]
        hallucinated = set(validation["hallucinated"])
        corrected_count = 0
        removed_count = 0

        surviving_changes = []
        for fc in code_proposal.file_changes:
            norm = fc.file_path.replace("\\", "/").lstrip("/")

            # New files are always kept — they're expected to not exist yet
            if fc.change_type == ChangeType.CREATE:
                surviving_changes.append(fc)
                continue

            if norm in hallucinated:
                # Remove entirely — this path doesn't exist in the real codebase
                removed_count += 1
                self.logger.warning(
                    "hallucinated_path_removed",
                    ticket_id=ticket_id,
                    path=fc.file_path,
                )
                continue

            if norm in corrections:
                # Correct to the real path
                old_path = fc.file_path
                fc.file_path = corrections[norm]
                corrected_count += 1
                self.logger.info(
                    "fuzzy_path_corrected",
                    ticket_id=ticket_id,
                    old=old_path,
                    new=fc.file_path,
                )

            surviving_changes.append(fc)

        # Mutate the proposal in-place
        code_proposal.file_changes = surviving_changes

        self.logger.info(
            "file_validation_applied",
            ticket_id=ticket_id,
            run_id=run_id,
            corrected=corrected_count,
            removed=removed_count,
            surviving=len(surviving_changes),
        )

        return {
            "code_proposal": code_proposal,
            "current_phase": WorkflowPhase.SUGGESTING_TESTS,
            "file_validation_result": {
                **validation,
                "corrected_count": corrected_count,
                "removed_count": removed_count,
                "surviving_count": len(surviving_changes),
            },
        }


_agent = FileValidatorAgent()


def file_validator_node(state: WorkflowState) -> dict:
    return _agent.run(state)
