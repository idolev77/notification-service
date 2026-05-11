"""
Notification model — PRD §2.3.

Relationships:
  - Notification (N) >── UserPreferences (1)   via `recipient_user_id`
  - Notification (1) ──< Delivery (N)          one row per fanned-out channel

Lifecycle (PRD §4.1):
    RECEIVED → PROCESSING → (per-channel Deliveries) → COMPLETED / FAILED
The top-level `status` here is independent of per-channel `Delivery.status`.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import NotificationPriority, NotificationStatus

if TYPE_CHECKING:
    from app.models.delivery import Delivery
    from app.models.user_preferences import UserPreferences


class Notification(Base, TimestampMixin):
    """
    A user-facing notification request.

    Field-by-field mapping to PRD §2.3:
      - `id`                   → "Unique identifier"     (UUIDv4, server-generated)
      - `recipient_user_id` /
        `recipient_contact`    → "Recipient (user ID or contact info)"
                                 Exactly one of these is populated; the API
                                 layer enforces XOR validation.
      - `notification_type`    → "Notification type"
      - `content`              → "Content / template reference"
                                 Inline content. Templates are resolved
                                 lazily by the dispatcher via
                                 (notification_type, channel) lookup
                                 because a single notification fans out
                                 to multiple channels with different
                                 per-channel templates (see DECISIONS.md
                                 §1). No request-level `template_id`.
      - `variables`            → "Variables (for template substitution)"
      - `priority`             → "Priority (high, normal, low)"
      - `status`               → "Status (received, processing, completed, failed)"
      - `scheduled_at`         → "Scheduled at (optional)"
      - `created_at`           → "Created at"   (provided by TimestampMixin)
    """

    __tablename__ = "notifications"

    # --- Identity ----------------------------------------------------------
    # WHY UUID (not autoincrement int): notification IDs travel through the
    # public API and webhooks — they must be unguessable.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # --- Recipient (XOR: user_id OR contact) ------------------------------
    # Nullable FK: a notification may target a raw contact (e.g. anonymous
    # password-reset email) where no user record exists.
    recipient_user_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        ForeignKey("user_preferences.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Free-form contact (email/phone/device-token/URL) when no user_id is given.
    recipient_contact: Mapped[Optional[str]] = mapped_column(
        String(512),
        nullable=True,
    )

    # --- Type & content ----------------------------------------------------
    notification_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )

    # Inline body. When NULL, the dispatcher resolves the active Template
    # for (notification_type, channel) at delivery time.
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # JSONB so callers can pass arbitrary variable trees, e.g.
    #   {"user": {"name": "Ada"}, "order": {"id": 42}}
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # --- Priority & status -------------------------------------------------
    priority: Mapped[NotificationPriority] = mapped_column(
        String(8),
        nullable=False,
        default=NotificationPriority.NORMAL,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        String(16),
        nullable=False,
        default=NotificationStatus.RECEIVED,
        index=True,
    )

    # --- Scheduling --------------------------------------------------------
    # NULL = send immediately. Non-NULL = defer until this UTC instant
    # (PRD §4.8). Indexed for the beat scheduler's "due now" query.
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # --- Relationships -----------------------------------------------------
    # N-to-1 back to the recipient user (optional — see XOR above).
    recipient_user: Mapped[Optional["UserPreferences"]] = relationship(
        back_populates="notifications",
    )

    # 1-to-N: one Notification fans out into one Delivery row per channel.
    # `cascade="all, delete-orphan"` keeps deliveries' lifetime tied to parent.
    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} "
            f"type={self.notification_type!r} status={self.status}>"
        )
