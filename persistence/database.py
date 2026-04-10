# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from persistence.models import Base


def _get_db_url() -> str:
    db_path = settings.sqlite_db_path
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    return f"sqlite:///{db_path}"


engine = create_engine(
    _get_db_url(),
    connect_args={"check_same_thread": False},
    echo=settings.db_echo,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db_session() -> Iterator[Session]:
    """Context manager for database sessions with auto-rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
