"""
SQLAlchemy declarative base + reusable mixins.

Kept tiny on purpose: every model imports `Base` from here so Alembic's
autogenerate sees a single MetaData object.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """
    Return a timezone-aware UTC `datetime`.

    WHY a helper: SQLAlchemy's `func.now()` runs in DB time, which may not be
    UTC depending on the server. Generating timestamps in Python guarantees
    UTC across all environments and makes tests deterministic.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Single declarative base shared by every ORM model in the project."""


class TimestampMixin:
    """
    Adds `created_at` / `updated_at` to any model that mixes it in.

    `created_at` satisfies PRD §2.3 ("Created at") for `Notification` and is a
    free audit trail for every other table.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
