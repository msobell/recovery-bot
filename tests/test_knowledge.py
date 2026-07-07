"""Tests for the PDF document knowledge base (separate corpus DB)."""
from __future__ import annotations

import pytest

from recovery.ingest import pdf as pdf_mod


# ── Chunking (pure, no DB) ───────────────────────────────────────────────────

def test_chunk_structural_split():
    text = (
        "# Deadlift\n\nKeep a neutral spine and brace hard. " + ("Drive through the heels. " * 20) +
        "\n# Bench Press\n\nRetract the scapula. " + ("Press evenly. " * 20)
    )
    chunks = pdf_mod.chunk_text(text)
    assert len(chunks) >= 2
    assert any("Deadlift" in c for c in chunks)
    assert any("Bench Press" in c for c in chunks)


def test_chunk_paragraph_fallback():
    # No headers/numbered sections — must fall back to blank-line paragraphs.
    text = ("First topic. " * 30) + "\n\n" + ("Second topic. " * 30)
    chunks = pdf_mod.chunk_text(text)
    assert len(chunks) >= 2


def test_chunk_hard_split_caps_size():
    text = ("This is a sentence. " * 400)  # ~8000 chars, no structure
    chunks = pdf_mod.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= pdf_mod.MAX_CHUNK_CHARS + 50 for c in chunks)


def test_chunk_empty():
    assert pdf_mod.chunk_text("   ") == []


def test_ocr_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pdf_mod.OcrUnavailable):
        pdf_mod.ocr_page(b"\x89PNG", "claude-haiku-4-5")


# ── Ingest / search / delete + DB separation ─────────────────────────────────

def _make_pdf(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def isolated_dbs(tmp_path, monkeypatch):
    """Point both recovery.db and knowledge.db at fresh temp files."""
    import recovery.db.session as s
    monkeypatch.setattr(s, "_DB_PATH", tmp_path / "recovery.db")
    monkeypatch.setattr(s, "KNOWLEDGE_DB_PATH", tmp_path / "knowledge.db")
    s._engines.clear()
    yield
    s._engines.clear()


def test_ingest_search_delete_and_separation(isolated_dbs):
    from recovery.knowledge.ingest import ingest_pdf, list_documents, search_corpus, delete_document
    from recovery.mcp.memory_tools import save_memory, query_memory

    pdf = _make_pdf(
        "Deadlift setup: brace the core, neutral spine, bar over midfoot. "
        "Drive the floor away and keep the bar against the legs."
    )
    summary = ingest_pdf("lifting.pdf", pdf)
    assert summary["chunks"] >= 1
    assert summary["used_ocr"] is False
    assert summary["filename"] == "lifting.pdf"

    docs = list_documents()
    assert len(docs) == 1
    assert docs[0]["filename"] == "lifting.pdf"

    # Corpus search finds the chunk, with citation metadata.
    hits = search_corpus("how do I brace for a deadlift", n_results=3)
    assert hits and hits[0]["source"] == "lifting.pdf"
    assert hits[0]["page"] == 1

    # SEPARATION: a personal memory and a corpus chunk must not cross over.
    save_memory("I prefer fasted morning cardio on rest days.", entities=["cardio"])
    # query_memory hits recovery.db only — must not return the PDF chunk.
    mem_res = query_memory("deadlift brace neutral spine", n_results=5)
    assert "lifting.pdf" not in mem_res
    assert "bar over midfoot" not in mem_res
    # search_corpus hits knowledge.db only — must not return the personal note.
    corpus_res = search_corpus("fasted morning cardio rest days", n_results=5)
    assert all("fasted" not in (h["content"] or "").lower() for h in corpus_res)

    # Delete removes the doc from the corpus.
    removed = delete_document(summary["doc_id"])
    assert removed == summary["chunks"]
    assert list_documents() == []


def test_delete_document_rejects_non_uuid(isolated_dbs):
    """LIKE wildcards in the doc_id ('%', '_') used to match — and delete —
    every chunk in the corpus."""
    from recovery.knowledge.ingest import ingest_pdf, list_documents, delete_document

    pdf = _make_pdf("Deload week: cut volume roughly in half and keep the bar speed crisp.")
    summary = ingest_pdf("deload.pdf", pdf)
    assert summary["chunks"] >= 1

    assert delete_document("%") == 0
    assert delete_document("_") == 0
    assert delete_document("%25") == 0
    assert delete_document("not-a-uuid") == 0
    assert len(list_documents()) == 1  # corpus untouched

    assert delete_document(summary["doc_id"]) == summary["chunks"]
    assert list_documents() == []


def test_ingest_rejects_empty_text_pdf(isolated_dbs, monkeypatch):
    # A PDF with no text layer and OCR disabled -> ValueError (clean 400 upstream).
    from recovery.knowledge.ingest import ingest_pdf
    import recovery.config as cfg_mod

    cfg = cfg_mod.get()
    monkeypatch.setattr(cfg.knowledge, "ocr_enabled", False)

    import fitz
    doc = fitz.open()
    doc.new_page()  # blank page, no text
    blank = doc.tobytes()
    doc.close()

    with pytest.raises(ValueError):
        ingest_pdf("blank.pdf", blank)
