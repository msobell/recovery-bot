from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def load_sqlite_vec(engine: Engine) -> None:
    """Register sqlite-vec on every new connection for this engine."""
    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _):
        try:
            import sqlite_vec
            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
        except ImportError:
            logger.warning("sqlite-vec not installed — vector search disabled.")
        except Exception as e:
            logger.error(f"Failed to load sqlite-vec: {e}")


def ensure_virtual_tables(session: Session) -> None:
    """Create FTS5 and vec0 virtual tables if they don't exist."""
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
