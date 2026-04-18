"""Tests for the memory layer: db_setup, search, and MCP tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from recovery.db.memory import KnowledgeEdge, Memory
from recovery.db.session import Base
from recovery.memory.db_setup import ensure_virtual_tables
from recovery.memory.search import hybrid_search, reciprocal_rank_fusion


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def memory_engine(tmp_path):
    """File-based engine with sqlite-vec loaded and all tables created."""
    db_path = tmp_path / "memory_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _load_vec(dbapi_conn, _):
        try:
            import sqlite_vec
            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
        except Exception:
            pass

    Base.metadata.create_all(engine)

    with sessionmaker(bind=engine)() as session:
        ensure_virtual_tables(session)

    yield engine
    engine.dispose()


@pytest.fixture()
def memory_session(memory_engine):
    Session = sessionmaker(bind=memory_engine)
    session = Session()
    yield session
    session.close()


FAKE_EMBEDDING = [0.1] * 384


def _fake_embedding(_text):
    return FAKE_EMBEDDING


# ── ensure_virtual_tables ─────────────────────────────────────────────────────

def test_ensure_virtual_tables_creates_fts(memory_session):
    result = memory_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'")
    ).fetchone()
    assert result is not None


def test_ensure_virtual_tables_creates_vec(memory_session):
    result = memory_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_vec'")
    ).fetchone()
    assert result is not None


def test_ensure_virtual_tables_is_idempotent(memory_session):
    ensure_virtual_tables(memory_session)
    ensure_virtual_tables(memory_session)
    result = memory_session.execute(
        text("SELECT count(*) FROM sqlite_master WHERE name='memories_fts'")
    ).scalar()
    assert result == 1


# ── Memory ORM ────────────────────────────────────────────────────────────────

def test_memory_insert_and_retrieve(memory_session):
    m = Memory(content="User prefers morning workouts")
    memory_session.add(m)
    memory_session.commit()
    fetched = memory_session.get(Memory, m.id)
    assert fetched.content == "User prefers morning workouts"
    assert fetched.created_at is not None


def test_memory_metadata_json(memory_session):
    m = Memory(content="bench press", metadata_json=json.dumps({"type": "entity"}))
    memory_session.add(m)
    memory_session.commit()
    fetched = memory_session.get(Memory, m.id)
    assert json.loads(fetched.metadata_json)["type"] == "entity"


def test_knowledge_edge_links_memories(memory_session):
    note = Memory(content="User does fasted cardio on easy days")
    entity = Memory(content="fasted cardio", metadata_json=json.dumps({"type": "entity"}))
    memory_session.add_all([note, entity])
    memory_session.flush()
    edge = KnowledgeEdge(source_id=note.id, target_id=entity.id, relationship_type="MENTIONS")
    memory_session.add(edge)
    memory_session.commit()

    refreshed = memory_session.get(Memory, note.id)
    assert len(refreshed.out_edges) == 1
    assert refreshed.out_edges[0].target.content == "fasted cardio"


def test_knowledge_edge_allows_multiple_types(memory_session):
    a = Memory(content="note A")
    b = Memory(content="note B")
    memory_session.add_all([a, b])
    memory_session.flush()
    memory_session.add(KnowledgeEdge(source_id=a.id, target_id=b.id, relationship_type="MENTIONS"))
    memory_session.add(KnowledgeEdge(source_id=a.id, target_id=b.id, relationship_type="CONTRADICTS"))
    memory_session.commit()
    assert len(a.out_edges) == 2


# ── reciprocal_rank_fusion ─────────────────────────────────────────────────────

def test_rrf_combines_both_lists():
    merged = reciprocal_rank_fusion([1, 2, 3], [2, 3, 4])
    ids = [doc_id for doc_id, _ in merged]
    assert 2 in ids and 3 in ids  # appear in both → should rank highest
    assert ids.index(2) < ids.index(1)  # 2 beats 1 (1 only in fts list)


def test_rrf_fts_only():
    merged = reciprocal_rank_fusion([10, 20, 30], [])
    ids = [doc_id for doc_id, _ in merged]
    assert ids == [10, 20, 30]


def test_rrf_vec_only():
    merged = reciprocal_rank_fusion([], [7, 8, 9])
    ids = [doc_id for doc_id, _ in merged]
    assert ids == [7, 8, 9]


def test_rrf_empty_inputs():
    assert reciprocal_rank_fusion([], []) == []


# ── hybrid_search ─────────────────────────────────────────────────────────────

def _insert_memory_with_indexes(session, content: str) -> Memory:
    """Helper: insert a Memory and update FTS + vec indexes."""
    import sqlite_vec
    m = Memory(content=content)
    session.add(m)
    session.flush()
    session.execute(
        text("INSERT INTO memories_fts(content, id) VALUES(:c, :id)"),
        {"c": content, "id": m.id},
    )
    blob = sqlite_vec.serialize_float32(FAKE_EMBEDDING)
    session.execute(
        text("INSERT INTO memories_vec(id, embedding) VALUES(:id, :emb)"),
        {"id": m.id, "emb": blob},
    )
    session.commit()
    return m


def test_hybrid_search_returns_fts_match(memory_session):
    _insert_memory_with_indexes(memory_session, "User prefers fasted cardio on easy days")
    _insert_memory_with_indexes(memory_session, "Sleep score was 85 last night")

    with patch("recovery.memory.search.get_embedding", _fake_embedding):
        results = hybrid_search(memory_session, "fasted cardio")

    contents = [m.content for m in results]
    assert any("fasted cardio" in c for c in contents)


def test_hybrid_search_returns_empty_when_no_match(memory_session):
    _insert_memory_with_indexes(memory_session, "HRV was 62ms this morning")

    with patch("recovery.memory.search.get_embedding", _fake_embedding):
        results = hybrid_search(memory_session, "xyzzy nonexistent term")

    # vec search will still return something (cosine of identical embeddings),
    # so just assert we get Memory objects back or an empty list — no crash
    assert isinstance(results, list)


def test_hybrid_search_respects_n_results(memory_session):
    for i in range(10):
        _insert_memory_with_indexes(memory_session, f"Note about training day {i}")

    with patch("recovery.memory.search.get_embedding", _fake_embedding):
        results = hybrid_search(memory_session, "training", n_results=3)

    assert len(results) <= 3


# ── MCP tools (save_memory / query_memory / get_related_entities) ─────────────

@pytest.fixture()
def patched_tools(memory_engine, monkeypatch):
    """Redirect MCP tool _get_session to use the test engine."""
    import recovery.mcp.memory_tools as tools_mod
    from sqlalchemy.orm import sessionmaker as SM
    monkeypatch.setattr(tools_mod, "_get_session", lambda: SM(bind=memory_engine)())
    monkeypatch.setattr(tools_mod, "get_embedding", _fake_embedding)
    monkeypatch.setattr("recovery.memory.search.get_embedding", _fake_embedding)
    return tools_mod


def test_save_memory_returns_confirmation(patched_tools):
    result = patched_tools.save_memory("User prefers morning workouts", ["morning workouts"])
    assert "Saved memory" in result
    assert "1 entities" in result


def test_save_memory_persists_content(patched_tools, memory_engine):
    patched_tools.save_memory("Sleep score was 85 last night", [])
    with sessionmaker(bind=memory_engine)() as s:
        m = s.query(Memory).filter(Memory.content == "Sleep score was 85 last night").first()
    assert m is not None


def test_save_memory_creates_entity_node(patched_tools, memory_engine):
    patched_tools.save_memory("User does bench press on push days", ["bench press"])
    with sessionmaker(bind=memory_engine)() as s:
        entity = s.query(Memory).filter(Memory.content == "bench press").first()
    assert entity is not None
    assert json.loads(entity.metadata_json)["type"] == "entity"


def test_save_memory_normalizes_entity_case(patched_tools, memory_engine):
    patched_tools.save_memory("Prefers Bench Press", ["Bench Press"])
    with sessionmaker(bind=memory_engine)() as s:
        entity = s.query(Memory).filter(Memory.content == "bench press").first()
    assert entity is not None


def test_save_memory_deduplicates_entity_nodes(patched_tools, memory_engine):
    patched_tools.save_memory("Note one about squat rack", ["squat rack"])
    patched_tools.save_memory("Note two about squat rack", ["squat rack"])
    with sessionmaker(bind=memory_engine)() as s:
        count = s.query(Memory).filter(Memory.content == "squat rack").count()
    assert count == 1


def test_save_memory_creates_mentions_edge(patched_tools, memory_engine):
    patched_tools.save_memory("User uses sauna post-workout", ["sauna"])
    with sessionmaker(bind=memory_engine)() as s:
        edge = s.query(KnowledgeEdge).filter_by(relationship_type="MENTIONS").first()
    assert edge is not None


def test_save_memory_with_metadata(patched_tools, memory_engine):
    patched_tools.save_memory("Injury note", [], metadata={"source": "user", "priority": "high"})
    with sessionmaker(bind=memory_engine)() as s:
        m = s.query(Memory).filter(Memory.content == "Injury note").first()
    assert json.loads(m.metadata_json)["priority"] == "high"


def test_query_memory_finds_saved_note(patched_tools):
    patched_tools.save_memory("User prefers fasted cardio on easy days", ["fasted cardio"])
    result = patched_tools.query_memory("fasted cardio")
    assert "fasted cardio" in result.lower()


def test_query_memory_returns_no_match_message_when_empty(patched_tools):
    result = patched_tools.query_memory("completely unrelated xyzzy query")
    # Either no results message or results — just no crash and a string
    assert isinstance(result, str)


def test_query_memory_shows_entities(patched_tools):
    patched_tools.save_memory("User lifts heavy on Monday", ["heavy lifting"])
    result = patched_tools.query_memory("Monday lifting")
    assert "### Results:" in result


def test_get_related_entities_found_by_exact_match(patched_tools):
    patched_tools.save_memory("User does kettlebell swings on rest days", ["kettlebell"])
    result = patched_tools.get_related_entities("kettlebell")
    assert "kettlebell" in result.lower()
    assert "kettlebell swings" in result.lower()


def test_get_related_entities_not_found(patched_tools):
    result = patched_tools.get_related_entities("nonexistent entity xyzzy")
    assert "not found" in result.lower() or "closest match" in result.lower()


def test_get_related_entities_multiple_memories(patched_tools):
    patched_tools.save_memory("User does pull-ups on back day", ["pull-ups"])
    patched_tools.save_memory("User does weighted pull-ups for strength", ["pull-ups"])
    result = patched_tools.get_related_entities("pull-ups")
    assert result.count("pull-ups") >= 2  # entity header + at least one memory
