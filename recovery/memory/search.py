from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from recovery.db.memory import Memory
from recovery.memory.embeddings import get_embedding

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    fts_ids: List[int],
    vec_ids: List[int],
    k: int = 60,
) -> List[Tuple[int, float]]:
    scores: Dict[int, float] = {}
    for rank, doc_id in enumerate(fts_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, doc_id in enumerate(vec_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def hybrid_search(session: Session, query: str, n_results: int = 10) -> List[Memory]:
    fts_rows = session.execute(
        text("SELECT id FROM memories_fts WHERE content MATCH :q LIMIT :n"),
        {"q": query, "n": n_results},
    ).fetchall()
    fts_ids = [row[0] for row in fts_rows]

    vec_ids = []
    try:
        import sqlite_vec
        query_blob = sqlite_vec.serialize_float32(get_embedding(query))
        vec_rows = session.execute(
            text("""
                SELECT id FROM memories_vec
                ORDER BY vec_distance_cosine(embedding, :qv)
                LIMIT :n
            """),
            {"qv": query_blob, "n": n_results},
        ).fetchall()
        vec_ids = [row[0] for row in vec_rows]
    except Exception as e:
        logger.warning(f"Vector search failed, using FTS only: {e}")

    merged = reciprocal_rank_fusion(fts_ids, vec_ids)
    top_ids = [doc_id for doc_id, _ in merged[:n_results]]

    if not top_ids:
        return []

    memories = session.query(Memory).filter(Memory.id.in_(top_ids)).all()
    memory_map = {m.id: m for m in memories}
    return [memory_map[mid] for mid in top_ids if mid in memory_map]
