from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# sqlite-vec is loaded per-connection by the engine's connect hook in
# recovery.db.session.get_engine — the single copy of that logic.


def ensure_virtual_tables(session: Session) -> None:
    """Create FTS5 and vec0 virtual tables if they don't exist.

    NOTE: commits the session (DDL must persist even on read-only paths).
    Call it before queuing any other pending changes.
    """
    session.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            id UNINDEXED,
            tokenize="trigram"
        )
    """))
    try:
        session.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                id INTEGER PRIMARY KEY,
                embedding float[384]
            )
        """))
    except Exception as e:
        logger.warning(f"vec0 table unavailable (sqlite-vec not loaded?): {e}")
    session.commit()
    logger.debug("Memory virtual tables ensured.")
