"""Session access for the document-corpus DB (knowledge.db).

Mirrors recovery.mcp.memory_tools._get_session but points at knowledge.db, so
corpus reads/writes physically never touch the personal-memory DB.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def get_knowledge_session() -> Session:
    from recovery.db.session import get_session, init_knowledge_db
    engine = init_knowledge_db()
    return get_session(engine)
