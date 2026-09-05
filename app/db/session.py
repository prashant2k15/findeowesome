"""Engine/session factory plus schema bootstrap."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import migrate
from app.db.models import Base

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if missing. Safe to call on every container start."""
    Base.metadata.create_all(engine)
    migrate.apply(engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(text("PRAGMA journal_mode=WAL"))


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
