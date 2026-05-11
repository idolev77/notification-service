"""
SQLAlchemy engine + session factory.

Design notes (defensible at oral review):
  - Synchronous SQLAlchemy is used deliberately:
      * Celery tasks are sync — using async sessions inside them adds a
        thread-pool / event-loop indirection with zero gain.
      * FastAPI handlers can call sync DB code via `run_in_threadpool`
        (FastAPI does this automatically for non-async path operations
        and dependencies). This keeps the data layer uniform across
        the API process and the worker process.
  - One engine per process (`lru_cache`); pool is `pool_pre_ping`'d so
    stale connections after a DB restart don't surface as 500s.
  - The session is exposed via a FastAPI dependency that guarantees
    rollback-on-exception and close-always semantics.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    Process-wide SQLAlchemy engine.

    `pool_pre_ping=True`: cheap `SELECT 1` on checkout to avoid using a
    connection that the DB closed (e.g. after a deploy/restart).
    `pool_recycle=1800`: defensive recycle every 30 min to dodge any
    middlebox idle-timeout (PG, PgBouncer, NLB, etc.).
    """
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Cached session factory bound to the cached engine."""
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    Transactional context manager for use OUTSIDE FastAPI (Celery tasks,
    scripts, alembic data migrations).

    Commits on clean exit; rolls back on any exception; always closes.
    This is the *one* place transaction boundaries are defined for
    background work — call sites must NOT call `commit()` manually.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session() -> Iterator[Session]:
    """
    FastAPI dependency.

    Usage:
        @router.post(...)
        def handler(db: Session = Depends(get_db_session)): ...

    Note: FastAPI handlers explicitly opt into commit by calling
    `db.commit()` themselves (POST/PATCH/DELETE), so reads stay cheap and
    write boundaries remain visible in the route code.
    Rollback-on-exception is still guaranteed.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
