# Recovery Bot — Claude Notes

## Project Overview

A personal recovery-monitoring bot that ingests Garmin and Strava data, scores daily readiness, and surfaces recommendations via a local web dashboard and an MCP server.

## Activities & Dedup

`garmin_activities` holds **all** Garmin activity types, not just strength:
- **Strength** (`is_strength=1`, sport_type in strength_training/weight_training):
  carry detailed `sets`; fetched by `garmin.fetch_strength_activities` +
  `_upsert_strength` (which never wipes existing sets or touches
  `manually_edited` sessions).
- **Cardio/other** (`is_strength=0`): summary fields only (start_time,
  distance, HR, calories); fetched by `garmin.fetch_cardio_activities` +
  `_upsert_garmin_activity`. Garmin is the source of truth, so the app no
  longer depends on Garmin→Strava forwarding to surface non-strength workouts.

Both are ingested in every sync path (daily_sync, backfill, MCP
`sync_missing_days`, CLI `sync-missing`).

**Dedup** (`analysis/dedupe.strava_duplicate_ids`): a Strava activity is hidden
if it mirrors ANY Garmin activity (±30 min start, or same-date for Garmin
activities lacking a start time). Timezone care: Strava start_time and Garmin
CARDIO start_time are local-naive (compare directly); Garmin STRENGTH start
comes from set rows in UTC and is converted to local first. `/api/activities`
lists Garmin activities (authoritative) plus non-dup Strava, newest first.

Schema note: `session.init_db` runs additive column migrations
(`_add_missing_columns`) since there's no Alembic — new nullable columns on
existing tables are ALTER-added there. Existing rows get NULL for a new column;
`is_strength` was backfilled to 1 for pre-existing (strength-only) rows.

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
  ingest/       # Garmin + Strava sync; pdf.py (PDF extract/chunk/OCR)
  knowledge/    # Document corpus: session + ingest/list/delete/search (knowledge.db)
  mcp/          # MCP server + memory tools
  memory/       # Embedding, search, and virtual-table setup
config.toml     # User config (timezone, equipment, thresholds)
```

## Document Knowledge Base (separate DB)

PDFs uploaded via the dashboard **Documents** tab are chunked, embedded, and
indexed into a **second SQLite database**, `~/.recovery-bot/knowledge.db`, kept
fully separate from personal memory (`recovery.db`) so general reference text
never conflates with hand-curated notes.

- `knowledge.db` reuses the **same schema** as the memory DB (`Memory` /
  `KnowledgeEdge` + `memories_fts` + `memories_vec`), so `ensure_virtual_tables`,
  the ORM models, and `hybrid_search` all apply unchanged against it.
- Engine selection is by path: `get_engine(db_path)` caches one engine per path
  (`recovery/db/session.py`); `init_knowledge_db()` + `knowledge/session.py`
  point at `knowledge.db`.
- Separation is **physical, not a filter**: `query_memory`/`save_memory` →
  `recovery.db`; `search_documents`/ingest → `knowledge.db`. A memory query can
  never surface a PDF chunk and vice versa.
- Corpus chunks carry `metadata_json` with `{source, doc_id, page, chunk_index}`
  and are **not** entity-linked (no knowledge-graph edges). Delete a doc by
  removing its rows from `memories_fts`, `memories_vec`, `memories` (matched by
  `doc_id`). `rm ~/.recovery-bot/knowledge.db` rebuilds the corpus from scratch.
- Scanned/image-only pages are OCR'd via Claude Haiku (`recovery/ingest/pdf.py`,
  `ocr_page`); needs `ANTHROPIC_API_KEY`, lazy-imported, OCR path only.

## Code Audit (2026-07-02) — all findings fixed 2026-07-03…06

A full-codebase + MCP review found ~35 issues (7 high-severity); **all are
fixed** and covered by regression tests where practical (166 tests, up from
140). The durable lessons are folded into "Gotchas" below. Highlights of what
was fixed, for archaeology:

- Ingest: Strava incremental sync skipped same-day activities (all three sync
  paths now overlap the window by a day; upsert is idempotent); a failed
  exercise-set fetch wiped previously synced strength sets (now `sets=None`
  signals failure and non-empty sets are never replaced with empty); Garmin
  fetchers swallowed errors silently (now logged); backfill re-ran the full
  window when already current; rate-limit delay now applies per API call and
  the sleep payload is fetched once per day instead of 3×.
- Memory/knowledge: FTS5 `MATCH` crashed on apostrophes/hyphens/parens (query
  is now token-quoted + branch wrapped); `DELETE /api/documents/%25` could
  wipe the whole corpus (doc_id now UUID-validated + matched via
  `json_extract`); `save_memory` lost the note on duplicate entities after
  normalization; entity lookups now filter on `{"type": "entity"}` and entity
  nodes are vec-indexed too; `get_related_entities` works on a fresh DB and
  its fuzzy fallback only accepts entity nodes.
- API/UI: PDF upload was the app's only `async` route and blocked the event
  loop for the whole ingest (now sync/threadpool); zero values were treated as
  missing across the sleep API; clearing a reps/weight input PATCHed 0 and
  locked it in as manually-edited (guarded client- and server-side with
  `Field(ge=1)`/`gt=0`); coach setup failures now return real HTTP errors
  instead of 200-with-error-text, `stop_reason` is checked, and the Save
  button can't save an error banner; untrusted names/filenames are HTML-escaped.
- MCP server: `get_exercise_history` crashed on NULL `start_time`;
  `query_date_range` validates metric names and dates; `sync_missing_days`
  check-then-start is now lock-guarded.
- Infra: engine cache is thread-safe, keyed on resolved paths, resolves
  `_DB_PATH` at call time (tests used to leak writes into the real
  `~/.recovery-bot/recovery.db`!), and skips redundant `create_all`;
  knowledge.db only gets Memory/KnowledgeEdge tables; config falls back to the
  repo-root config.toml when cwd differs; `mcp install`/`schedule install` use
  `sys.executable`; Strava token file is chmod 0600.

## Gotchas (learned the hard way — keep honoring these)

- **Garmin "Local" timestamps are fake epochs**: `sleepStartTimestampLocal`
  etc. are already shifted to local wall time and MUST be decoded as UTC
  (`garmin._from_garmin_local_ms`), never `datetime.fromtimestamp()`.
  Verified against the live API 2026-07-06. Historical rows ingested with the
  old decode were repaired in place (806 rows, DST-aware unshift); backup at
  `~/.recovery-bot/recovery.db.bak-20260706`.
- **Garmin's `avgSleepStress` doesn't match its own stress series** (observed
  25.0 vs a true window mean of 18.2), so `fetch_stress_detail` computes
  `overnight_stress_avg` from the sleep-window readings and overrides Garmin's
  number; Garmin's value is only a fallback when the series is unavailable.
  Rows before 2026-06-29 still carry Garmin's inflated averages — repairable
  only by re-syncing (`recovery sync-missing --days N`, ~8 s/day).
- **Body battery "at wake" is the post-recharge peak, not `values[0]`**:
  the series runs ~midnight→night and drains while awake / recharges during
  sleep. `fetch_body_battery` takes the reading nearest the GMT sleep-end
  timestamp (±90 min), falling back to the daily max. `values[0]` grabs the
  mid-sleep low and plain `max()` can grab a midday nap recharge.
- **Strava sync windows must overlap**: always fetch from
  `last_synced_date - 1 day`; the upsert is idempotent by `strava_id`. Never
  use `last + 1 day` — it permanently skips activities recorded later on the
  last-synced day. (This bug existed in three separate places.)
- **Never replace strength sets with nothing**: `_upsert_strength` treats
  `sets=None` as "fetch failed" and refuses to blank non-empty sets;
  `manually_edited` sessions are never touched.
- **The sleep-night header labels the conventional night span** (evening
  before → morning of `night_date`), not raw timestamps — a post-midnight
  bedtime would otherwise render "Sun → Sun".
- **MCP error convention**: memory tools return `"Error: ..."` strings (their
  callers — `log_strength_note`, the workout-save route — depend on this);
  data tools raise or return `{"error": ...}` dicts. Don't "unify" without
  updating the callers.
- **FastAPI routes here are sync `def` on purpose** (threadpool). Don't make a
  route `async` unless nothing in it blocks.
