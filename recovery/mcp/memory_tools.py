from __future__ import annotations

import json
import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from recovery.db.memory import KnowledgeEdge, Memory
from recovery.memory.db_setup import ensure_virtual_tables
from recovery.memory.embeddings import get_embedding
from recovery.memory.search import hybrid_search

logger = logging.getLogger(__name__)


def _get_session() -> Session:
    from recovery.db.session import get_session, init_db
    engine = init_db()
    return get_session(engine)


def save_memory(content: str, entities: List[str], metadata: Optional[dict] = None) -> str:
    """
    Save a note and link it to named entities (people, exercises, concepts, etc.).

    Indexes the note for both keyword and semantic search, and connects it to
    entity nodes in the knowledge graph via MENTIONS edges.
    """
    session = _get_session()
    try:
        ensure_virtual_tables(session)

        memory = Memory(
            content=content,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        session.add(memory)
        session.flush()

        session.execute(
            text("INSERT INTO memories_fts(content, id) VALUES(:c, :id)"),
            {"c": content, "id": memory.id},
        )

        try:
            import sqlite_vec
            blob = sqlite_vec.serialize_float32(get_embedding(content))
            session.execute(
                text("INSERT INTO memories_vec(id, embedding) VALUES(:id, :emb)"),
                {"id": memory.id, "emb": blob},
            )
        except Exception as e:
            logger.warning(f"Vector index skipped: {e}")

        for name in entities:
            normalized = name.strip().lower()
            entity = session.query(Memory).filter(
                Memory.content == normalized,
            ).first()
            if not entity:
                entity = Memory(content=normalized, metadata_json=json.dumps({"type": "entity"}))
                session.add(entity)
                session.flush()
                session.execute(
                    text("INSERT INTO memories_fts(content, id) VALUES(:c, :id)"),
                    {"c": normalized, "id": entity.id},
                )
            session.add(KnowledgeEdge(
                source_id=memory.id,
                target_id=entity.id,
                relationship_type="MENTIONS",
            ))

        session.commit()
        return f"Saved memory {memory.id}, linked to {len(entities)} entities."
    except Exception as e:
        session.rollback()
        logger.exception("save_memory failed")
        return f"Error: {e}"
    finally:
        session.close()


def query_memory(query: str, n_results: int = 5) -> str:
    """
    Search saved memories using hybrid keyword + semantic search.

    Use this to recall facts, preferences, past observations, or context
    that was previously saved. Returns the most relevant notes.
    """
    session = _get_session()
    try:
        ensure_virtual_tables(session)
        results = hybrid_search(session, query, n_results)
        if not results:
            return "No matching memories found."
        lines = ["### Results:"]
        for i, m in enumerate(results):
            lines.append(f"{i + 1}. [ID:{m.id}] {m.content}")
            if i < 3:
                entities = [e.target.content for e in m.out_edges if e.relationship_type == "MENTIONS"]
                if entities:
                    lines.append(f"   Entities: {', '.join(entities)}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("query_memory failed")
        return f"Error: {e}"
    finally:
        session.close()


def get_related_entities(entity_name: str) -> str:
    """
    Walk the knowledge graph for all memories and entities linked to a name.

    Use this when the user references a concept, exercise, or person and you
    want to surface everything saved about it.
    """
    session = _get_session()
    try:
        normalized = entity_name.strip().lower()
        entity = session.query(Memory).filter(Memory.content == normalized).first()
        if not entity:
            results = hybrid_search(session, entity_name, n_results=1)
            if not results:
                return f"'{entity_name}' not found."
            entity = results[0]
            lines = [f"Closest match: '{entity.content}'"]
        else:
            lines = [f"Related to '{entity_name}':"]

        mentions = [e.source.content for e in entity.in_edges if e.relationship_type == "MENTIONS"]
        if mentions:
            lines.append("\n**Memories mentioning this:**")
            lines.extend(f"- {m}" for m in mentions)

        related = [e.target.content for e in entity.out_edges if e.relationship_type == "MENTIONS"]
        if related:
            lines.append("\n**Also linked to:**")
            lines.extend(f"- {r}" for r in related)

        return "\n".join(lines) if len(lines) > 1 else f"No relations found for '{entity_name}'."
    except Exception as e:
        logger.exception("get_related_entities failed")
        return f"Error: {e}"
    finally:
        session.close()
