# Recovery Bot — Claude Notes

## Project Overview

A personal recovery-monitoring bot that ingests Garmin and Strava data, scores daily readiness, and surfaces recommendations via a local web dashboard and an MCP server.

## Memory Layer

The app uses a hybrid local knowledge base for saving and retrieving notes across sessions. It combines keyword search (FTS5/BM25) and semantic search (vector embeddings) fused via Reciprocal Rank Fusion (RRF). Everything runs locally — no external APIs.

### Stack

| Component | Role |
|---|---|
| SQLite (SQLAlchemy ORM) | Main relational store |
| FTS5 (`memories_fts`, trigram tokenizer) | Keyword / BM25 search |
| sqlite-vec (`memories_vec`, 384-dim vec0) | Vector / cosine similarity search |
| `sentence-transformers` (`all-MiniLM-L6-v2`) | Local embedding model (~80 MB, lazy-loaded) |

### Key Files

```
recovery/
  db/
    session.py          # SQLAlchemy engine + sqlite-vec loader, DB at ~/.recovery-bot/recovery.db
    memory.py           # Memory + KnowledgeEdge ORM models
  memory/
    db_setup.py         # ensure_virtual_tables() — creates FTS5 + vec0 tables on first use
    embeddings.py       # get_embedding() / get_embeddings() — lazy singleton
    search.py           # hybrid_search() — BM25 + cosine via RRF
  mcp/
    memory_tools.py     # MCP tools: save_memory, query_memory, get_related_entities
```

### Data Model

- **`memories`** — notes and entity nodes. Entity nodes are `Memory` rows with `metadata_json={"type": "entity"}`.
- **`knowledge_graph`** — directed edges (`source_id`, `target_id`, `relationship_type`). Currently only `MENTIONS` edges are created, linking a note to named entities (exercises, players, etc.).
- **`memories_fts`** — FTS5 virtual table mirroring `content` from `memories`.
- **`memories_vec`** — vec0 virtual table storing 384-dim embeddings per memory.

### Important Constraints

- Virtual tables (`memories_fts`, `memories_vec`) must **not** be created in `Base.metadata.create_all()` — sqlite-vec must be loaded first. `ensure_virtual_tables()` handles this lazily before the first memory operation.
- sqlite-vec is loaded on every connection via a SQLAlchemy `connect` event in `session.py`.
- Entity deduplication is **exact string match** — normalize casing before lookup if needed.
- FTS5 and vec0 don't support in-place updates cleanly; updates require delete + re-insert.
- To delete a memory, remove rows from `knowledge_graph`, `memories_fts`, `memories_vec`, and `memories` (in that order) — or use a Python script that loads sqlite-vec before connecting (raw `sqlite3` CLI lacks the extension).

### MCP Tools

| Tool | Description |
|---|---|
| `save_memory(content, entities, metadata)` | Write a note, index in FTS5 + vec0, upsert entity nodes, create MENTIONS edges |
| `query_memory(query, n_results)` | Hybrid search — returns ranked Memory rows |
| `get_related_entities(entity_name)` | Walk the knowledge graph from a named entity |

### Database Location

```
~/.recovery-bot/recovery.db
```

## Project Structure

```
recovery/
  api/          # FastAPI app + routes (dashboard, data)
  analysis/     # Recovery scoring logic
  db/           # ORM models, session, memory models
  ingest/       # Garmin + Strava sync
  mcp/          # MCP server + memory tools
  memory/       # Embedding, search, and virtual-table setup
config.toml     # User config (timezone, equipment, thresholds)
```
