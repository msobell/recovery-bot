from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_DB_PATH = Path.home() / ".recovery-bot" / "recovery.db"


class Base(DeclarativeBase):
    pass


def get_engine(db_path: Path = _DB_PATH):
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

    return engine


def init_db(engine=None):
    from recovery.db import models  # noqa: F401
    from recovery.db import memory  # noqa: F401 — registers Memory + KnowledgeEdge
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session(engine=None):
    if engine is None:
        engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
