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

        # De-dupe after normalization — ["Deadlift", "deadlift"] would create
        # two identical edge PKs and roll back the whole save
        normalized_entities = list(dict.fromkeys(
            n.strip().lower() for n in entities if n and n.strip()
        ))
        entity_meta = json.dumps({"type": "entity"})
        for normalized in normalized_entities:
            # Match only entity nodes — a note whose whole content equals the
            # name must not be linked as if it were the entity
            entity = session.query(Memory).filter(
                Memory.content == normalized,
                Memory.metadata_json == entity_meta,
            ).first()
            if not entity:
                entity = Memory(content=normalized, metadata_json=entity_meta)
                session.add(entity)
                session.flush()
                session.execute(
                    text("INSERT INTO memories_fts(content, id) VALUES(:c, :id)"),
                    {"c": normalized, "id": entity.id},
                )
                try:
                    import sqlite_vec
                    blob = sqlite_vec.serialize_float32(get_embedding(normalized))
                    session.execute(
                        text("INSERT INTO memories_vec(id, embedding) VALUES(:id, :emb)"),
                        {"id": entity.id, "emb": blob},
                    )
                except Exception as e:
                    logger.warning(f"Entity vector index skipped: {e}")
            session.add(KnowledgeEdge(
                source_id=memory.id,
                target_id=entity.id,
                relationship_type="MENTIONS",
            ))

        session.commit()
        return f"Saved memory {memory.id}, linked to {len(normalized_entities)} entities."
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
        ensure_virtual_tables(session)
        normalized = entity_name.strip().lower()
        entity_meta = json.dumps({"type": "entity"})
        entity = session.query(Memory).filter(
            Memory.content == normalized,
            Memory.metadata_json == entity_meta,
        ).first()
        if not entity:
            # Fuzzy fallback: only accept an actual entity node — a note's
            # edges point the other way and would be mislabeled here
            results = hybrid_search(session, entity_name, n_results=5)
            entity = next((m for m in results if m.metadata_json == entity_meta), None)
            if not entity:
                return f"'{entity_name}' not found."
            lines = [f"Closest match: '{entity.content}'"]
        else:
            lines = [f"Related to '{entity_name}':"]

        mentions = [e.source.content for e in entity.in_edges if e.relationship_type == "MENTIONS"]
        if mentions:
            lines.append("\n**Memories mentioning this:**")
            lines.extend(f"- {m}" for m in mentions)

        if len(lines) == 1:
            if entity.content != normalized:
                return f"No relations found; closest match '{entity.content}' has no linked memories."
            return f"No relations found for '{entity_name}'."
        return "\n".join(lines)
    except Exception as e:
        logger.exception("get_related_entities failed")
        return f"Error: {e}"
    finally:
        session.close()
