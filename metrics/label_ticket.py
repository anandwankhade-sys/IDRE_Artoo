# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
CLI tool for adding ground-truth labels for KPI 2.

Usage:
    python -m metrics.label_ticket PROJ-123 --truly-incomplete
    python -m metrics.label_ticket PROJ-123 --complete
    python -m metrics.label_ticket PROJ-123 --truly-incomplete --labeled-by "Alice" --notes "Missing AC"
"""

from __future__ import annotations

import argparse
import sys

from persistence.database import init_db
from persistence.repository import TicketRepository


def main():
    parser = argparse.ArgumentParser(description="Label Jira tickets for KPI 2 ground truth")
    parser.add_argument("ticket_id", help="Jira ticket ID (e.g. PROJ-123)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--truly-incomplete", action="store_true", help="Mark as truly incomplete")
    group.add_argument("--complete", action="store_true", help="Mark as complete (not incomplete)")
    parser.add_argument("--labeled-by", default="", help="Name of the person labeling")
    parser.add_argument("--notes", default="", help="Optional notes")

    args = parser.parse_args()

    init_db()
    repo = TicketRepository()
    truly_incomplete = args.truly_incomplete

    repo.set_ground_truth(
        ticket_id=args.ticket_id,
        truly_incomplete=truly_incomplete,
        labeled_by=args.labeled_by or None,
        notes=args.notes or None,
    )

    label = "TRULY INCOMPLETE" if truly_incomplete else "COMPLETE"
    print(f"[OK] Labeled {args.ticket_id} as {label}")


if __name__ == "__main__":
    main()
