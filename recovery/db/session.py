from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_DB_PATH = Path.home() / ".recovery-bot" / "recovery.db"
# Document corpus lives in its own DB so it stays fully separate from personal
# memory — different file, different connection. Delete it to rebuild the corpus.
KNOWLEDGE_DB_PATH = Path.home() / ".recovery-bot" / "knowledge.db"

# Cache engines by resolved path so repeated get_engine(<same path>) reuses one
# engine (and its sqlite-vec connect hook) instead of rebuilding it.
_engines: dict[str, object] = {}


class Base(DeclarativeBase):
    pass


def get_engine(db_path: Path = _DB_PATH):
    key = str(db_path)
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
    Base.metadata.create_all(engine)
    return engine


def init_knowledge_db():
    """Build (or reuse) the document-corpus engine and create its tables.

    Reuses the same ORM models as the main DB — knowledge.db holds Memory /
    KnowledgeEdge rows with the same schema, so all the memory-layer logic
    (virtual tables, hybrid search) applies unchanged against it.
    """
    return init_db(get_engine(KNOWLEDGE_DB_PATH))


def get_session(engine=None):
    if engine is None:
        engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
