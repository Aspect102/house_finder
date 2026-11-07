"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

_settings = get_settings()

_connect_args = {}
if _settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _migrate_sqlite() -> None:
    """Add columns introduced after the initial schema (SQLite only).

    ``create_all`` never alters existing tables, so we add missing columns
    defensively. This keeps upgrades from older databases working.
    """
    if not _settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "listings" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("listings")}
    additions = {
        "tenancy_start": "DATE",
        "available_now": "BOOLEAN DEFAULT 0",
        "dedupe_key": "VARCHAR(200) DEFAULT ''",
        "duplicate_of_id": "INTEGER",
    }
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE listings ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
