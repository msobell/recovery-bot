"""Ingest, list, delete, and search the document corpus (knowledge.db).

Each PDF becomes a set of chunk rows in the corpus DB's `memories` table,
indexed in `memories_fts` + `memories_vec`, grouped by a `doc_id`. The insert
sequence mirrors recovery.mcp.memory_tools.save_memory (minus entity linking).
Search reuses recovery.memory.search.hybrid_search verbatim against a corpus
session.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from recovery.config import get as get_config
from recovery.db.memory import Memory
from recovery.ingest import pdf as pdf_mod
from recovery.knowledge.session import get_knowledge_session
from recovery.memory.db_setup import ensure_virtual_tables
from recovery.memory.embeddings import get_embeddings
from recovery.memory.search import hybrid_search

logger = logging.getLogger(__name__)


def ingest_pdf(filename: str, pdf_bytes: bytes) -> dict:
    """Extract, chunk, and index a PDF into the corpus DB.

    Returns a summary dict. Raises ValueError if the PDF yields no usable text
    (e.g. fully scanned with no OCR key) so the API can return a clean 400.
    """
    cfg = get_config()
    pages, used_ocr = pdf_mod.extract_pages(
        pdf_bytes,
        ocr_enabled=cfg.knowledge.ocr_enabled,
        ocr_model=cfg.knowledge.ocr_model,
    )
    if not pages:
        raise ValueError(
            "No extractable text found. If this is a scanned PDF, set "
            "ANTHROPIC_API_KEY and enable OCR."
        )

    # Build (page, chunk) pairs across the whole document.
    chunk_items: list[tuple[int, str]] = []
    for page_no, page_text in pages:
        for chunk in pdf_mod.chunk_text(page_text):
            chunk_items.append((page_no, chunk))

    if not chunk_items:
        raise ValueError("PDF produced no chunks after extraction.")

    doc_id = str(uuid.uuid4())
    contents = [c for _, c in chunk_items]
    embeddings = get_embeddings(contents)  # one batched model call

    session = get_knowledge_session()
    try:
        ensure_virtual_tables(session)
        try:
            import sqlite_vec
            have_vec = True
        except Exception:
            have_vec = False

        for idx, ((page_no, content), emb) in enumerate(zip(chunk_items, embeddings)):
            mem = Memory(
                content=content,
                metadata_json=json.dumps({
                    "source": filename,
                    "doc_id": doc_id,
                    "page": page_no,
                    "chunk_index": idx,
                }),
            )
            session.add(mem)
            session.flush()  # assign mem.id

            session.execute(
                text("INSERT INTO memories_fts(content, id) VALUES(:c, :id)"),
                {"c": content, "id": mem.id},
            )
            if have_vec:
                # Degrade to FTS-only if the vec0 table is unavailable (the
                # import succeeding doesn't guarantee the extension loaded on
                # this connection) — mirrors save_memory
                try:
                    blob = sqlite_vec.serialize_float32(emb)
                    session.execute(
                        text("INSERT INTO memories_vec(id, embedding) VALUES(:id, :emb)"),
                        {"id": mem.id, "emb": blob},
                    )
                except Exception as e:
                    logger.warning("vector index insert failed, continuing FTS-only: %s", e)
                    have_vec = False

        session.commit()
        return {
            "doc_id": doc_id,
            "filename": filename,
            # Highest page number that yielded a chunk — same definition
            # list_documents uses, so upload and list agree
            "pages": max(p for p, _ in pages),
            "chunks": len(chunk_items),
            "used_ocr": used_ocr,
        }
    except Exception:
        session.rollback()
        logger.exception("ingest_pdf failed")
        raise
    finally:
        session.close()


def list_documents() -> list[dict]:
    """List corpus documents grouped by doc_id."""
    session = get_knowledge_session()
    try:
        ensure_virtual_tables(session)
        rows = session.query(Memory).filter(Memory.metadata_json.isnot(None)).all()
        docs: dict[str, dict] = {}
        for m in rows:
            try:
                meta = json.loads(m.metadata_json)
            except Exception:
                continue
            doc_id = meta.get("doc_id")
            if not doc_id:
                continue
            d = docs.setdefault(doc_id, {
                "doc_id": doc_id,
                "filename": meta.get("source", "unknown"),
                "chunks": 0,
                "pages": 0,
                "created_at": str(m.created_at) if m.created_at else None,
            })
            d["chunks"] += 1
            d["pages"] = max(d["pages"], meta.get("page", 0))
        return sorted(docs.values(), key=lambda d: d["created_at"] or "", reverse=True)
    finally:
        session.close()


def delete_document(doc_id: str) -> int:
    """Delete all chunks for a doc_id from the corpus DB. Returns rows removed."""
    # doc_ids are always UUIDs; reject anything else before it reaches a query
    # (LIKE-style matching on raw input allowed wildcard deletes)
    try:
        uuid.UUID(doc_id)
    except (ValueError, AttributeError, TypeError):
        return 0

    session = get_knowledge_session()
    try:
        ensure_virtual_tables(session)
        rows = session.query(Memory).filter(
            func.json_extract(Memory.metadata_json, "$.doc_id") == doc_id
        ).all()
        ids = [m.id for m in rows]
        if not ids:
            return 0
        for mid in ids:
            session.execute(text("DELETE FROM memories_fts WHERE id = :id"), {"id": mid})
            try:
                session.execute(text("DELETE FROM memories_vec WHERE id = :id"), {"id": mid})
            except Exception:
                pass
        for m in rows:
            session.delete(m)
        session.commit()
        return len(ids)
    except Exception:
        session.rollback()
        logger.exception("delete_document failed")
        raise
    finally:
        session.close()


def search_corpus(query: str, n_results: int = 5) -> list[dict]:
    """Hybrid search the corpus; returns chunks with source + page for citation."""
    session = get_knowledge_session()
    try:
        ensure_virtual_tables(session)
        results = hybrid_search(session, query, n_results)
        out = []
        for m in results:
            meta = {}
            if m.metadata_json:
                try:
                    meta = json.loads(m.metadata_json)
                except Exception:
                    pass
            out.append({
                "content": m.content,
                "source": meta.get("source"),
                "page": meta.get("page"),
                "doc_id": meta.get("doc_id"),
            })
        return out
    finally:
        session.close()
