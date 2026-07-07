import threading
import weakref
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_DB_PATH = Path.home() / ".recovery-bot" / "recovery.db"
# Document corpus lives in its own DB so it stays fully separate from personal
# memory — different file, different connection. Delete it to rebuild the corpus.
KNOWLEDGE_DB_PATH = Path.home() / ".recovery-bot" / "knowledge.db"

# Cache engines by resolved path so repeated get_engine(<same path>) reuses one
# engine (and its sqlite-vec connect hook) instead of rebuilding it. The lock
# keeps concurrent first requests from each building (and leaking) an engine.
_engines: dict[str, object] = {}
_engines_lock = threading.Lock()

# Engines whose tables have been created — skips redundant create_all() calls
# (a PRAGMA existence check per table) on every session acquisition.
_initialized = weakref.WeakSet()
_knowledge_initialized = weakref.WeakSet()


class Base(DeclarativeBase):
    pass


def get_engine(db_path: Path | None = None):
    # Resolve the default at call time, not def time, so tests (and anything
    # else) that monkeypatch _DB_PATH actually take effect
    if db_path is None:
        db_path = _DB_PATH
    key = str(Path(db_path).resolve())
    with _engines_lock:
        if key in _engines:
            return _engines[key]

        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}", echo=False)

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _):
            import logging
            try:
                import sqlite_vec
                dbapi_conn.enable_load_extension(True)
                sqlite_vec.load(dbapi_conn)
                dbapi_conn.enable_load_extension(False)
            except ImportError:
                logging.getLogger(__name__).warning("sqlite-vec not installed — vector search disabled.")
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to load sqlite-vec: {e}")

        _engines[key] = engine
        return engine


def init_db(engine=None):
    from recovery.db import models  # noqa: F401
    from recovery.db import memory  # noqa: F401 — registers Memory + KnowledgeEdge
    if engine is None:
        engine = get_engine()
    if engine not in _initialized:
        Base.metadata.create_all(engine)
        _add_missing_columns(engine)
        _initialized.add(engine)
    return engine


# Lightweight additive migrations for a single-file personal DB (no Alembic).
# create_all() never adds columns to an existing table, so newly-added nullable
# columns are backfilled here. Idempotent: only adds what's missing.
_COLUMN_MIGRATIONS = {
    "garmin_activities": {
        "start_time": "DATETIME",
        "distance_m": "FLOAT",
        "elevation_m": "FLOAT",
        "max_hr": "INTEGER",
        "calories": "INTEGER",
        "is_strength": "INTEGER DEFAULT 1",
    },
}


def _add_missing_columns(engine) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, cols in _COLUMN_MIGRATIONS.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                continue  # table doesn't exist yet (fresh DB handled by create_all)
            for name, decl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))


def init_knowledge_db():
    """Build (or reuse) the document-corpus engine and create its tables.

    knowledge.db holds ONLY Memory / KnowledgeEdge rows (same schema as the
    main DB, so virtual tables and hybrid search apply unchanged) — the app's
    other tables are deliberately not created here.
    """
    from recovery.db.memory import KnowledgeEdge, Memory
    engine = get_engine(KNOWLEDGE_DB_PATH)
    if engine not in _knowledge_initialized:
        Base.metadata.create_all(engine, tables=[Memory.__table__, KnowledgeEdge.__table__])
        _knowledge_initialized.add(engine)
    return engine


def get_session(engine=None):
    if engine is None:
        engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
