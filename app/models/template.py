"""
Template model — PRD §2.2.

Relationships:
  - Template stands alone (no FK relationships in Sprint 1).
    Notifications reference templates by `(notification_type, channel)`
    composite key at render time, not via a hard FK — this keeps a
    Notification's audit trail valid even if a Template is later edited
    or deactivated.

Uniqueness:
  - (notification_type, channel) must be unique among ACTIVE templates.
    Enforced by a partial unique index — see `__table_args__`.
"""

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import ChannelType


class Template(Base, TimestampMixin):
    """
    A renderable message template.

    Field-by-field mapping to PRD §2.2:
      - `notification_type` → "Notification type"
      - `channel`           → "Channel" (one template per channel per type)
      - `subject`           → "Subject (for email)"   (nullable for non-email)
      - `body`              → "Body (with placeholders, e.g. Hello {{user.name}})"
      - `is_active`         → "Active flag"
    """

    __tablename__ = "templates"

    # Surrogate PK so templates can be edited/versioned without breaking refs.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    notification_type: Mapped[str] = mapped_column(String(128), nullable=False)

    # Stored as the enum's string value via SQLAlchemy native enum support.
    channel: Mapped[ChannelType] = mapped_column(
        String(16),
        nullable=False,
    )

    # Email-only field; NULL for SMS/push/webhook templates.
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # `Text` (not `String`) because bodies can be long (HTML emails).
    body: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Optimistic concurrency control -----------------------------------
    # Auto-incremented by SQLAlchemy on every UPDATE; surfaced as the
    # `ETag` response header. Concurrent updates with a stale `If-Match`
    # value get a 412 Precondition Failed.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        # Partial unique index: only one ACTIVE template per (type, channel).
        # Inactive duplicates are allowed → enables soft-versioning.
        Index(
            "uq_active_template_per_type_channel",
            "notification_type",
            "channel",
            unique=True,
            postgresql_where=is_active.is_(True),
        ),
    )

    __mapper_args__ = {
        "version_id_col": version_id,
    }

    def __repr__(self) -> str:
        return (
            f"<Template id={self.id} "
            f"type={self.notification_type!r} channel={self.channel}>"
        )
