"""
Delivery model — PRD §2.4.

Relationships:
  - Delivery (N) >── Notification (1)   via `notification_id` FK.
    One Delivery row per (Notification × Channel) fan-out.

WHY a separate table (vs. extra columns on Notification):
  - Per-channel state machine (queued → sending → delivered/failed) lives
    independently per channel — required by PRD §4.1 and §4.4 (failure
    isolation between channels).
  - Retry counters and provider responses are channel-specific.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ChannelType, DeliveryStatus

if TYPE_CHECKING:
    from app.models.notification import Notification


class Delivery(Base, TimestampMixin):
    """
    A single attempt-tracking record for one Notification on one channel.

    Field-by-field mapping to PRD §2.4:
      - `notification_id`    → "Notification relationship (FK to Notification)"
      - `channel`            → "Channel"
      - `recipient_address`  → "Recipient address (email/phone/device token/URL)"
      - `status`             → "Status (queued, sending, delivered, failed)"
                               Plus `permanently_failed` per §4.1.
      - `attempts`           → "Attempts count"
      - `last_attempt_at`    → "Last attempt at"
      - `delivered_at`       → "Delivered at"
      - `error_message`      → "Error message (if failed)"
      - `provider_response`  → "Provider response (for debugging)"
    """

    __tablename__ = "deliveries"

    # UUID for the same reason as Notification: tracking IDs may be exposed.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # --- Parent link -------------------------------------------------------
    # CASCADE: if a notification is hard-deleted, its deliveries go with it.
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- Channel & address ------------------------------------------------
    channel: Mapped[ChannelType] = mapped_column(String(16), nullable=False)

    # Address shape varies per channel (email / E.164 phone / device token /
    # webhook URL). Validation happens in the channel-specific provider layer.
    recipient_address: Mapped[str] = mapped_column(String(2048), nullable=False)

    # --- State machine ----------------------------------------------------
    status: Mapped[DeliveryStatus] = mapped_column(
        String(24),
        nullable=False,
        default=DeliveryStatus.QUEUED,
        index=True,
    )

    # --- Attempt accounting -----------------------------------------------
    # Starts at 0; incremented by the worker at the start of each attempt.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # --- Diagnostics ------------------------------------------------------
    # Short human-readable reason; full structured payload lives in JSONB.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # --- Relationships ----------------------------------------------------
    notification: Mapped["Notification"] = relationship(back_populates="deliveries")

    def __repr__(self) -> str:
        return (
            f"<Delivery id={self.id} channel={self.channel} "
            f"status={self.status} attempts={self.attempts}>"
        )
