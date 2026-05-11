"""
UserPreferences model — PRD §2.1.

Relationships:
  - UserPreferences (1) ──< Notification (N)
    A user may receive many notifications; each notification belongs to
    exactly one recipient user. The relationship is wired from the
    `Notification` side via `recipient_user_id`.
"""

from datetime import time
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Integer, String, Time
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    # Imported only for type checkers to avoid a circular import at runtime.
    from app.models.notification import Notification


class UserPreferences(Base, TimestampMixin):
    """
    Stores a user's notification preferences.

    Field-by-field mapping to PRD §2.1:
      - `user_id`              → "User identifier"           (also primary key)
      - `enabled_channels`     → "Global enabled channels"   (subset of ChannelType)
      - `per_type_preferences` → "Per-type channel preferences"
                                 shape: {notification_type: [channel, ...]}
      - `quiet_hours_*`        → "Quiet hours configuration"
      - `frequency_caps`       → "Frequency cap settings"
                                 shape: {"per_hour": int, "per_day": int}
      - `webhook_url`          → "Webhook URL"

    Additional engineering field:
      - `is_paused` supports PRD §3.5 ("Pause / resume notifications for a user").
        Kept here because it is a per-user gate; surfacing it on the model
        avoids a separate table for a single boolean.
    """

    __tablename__ = "user_preferences"

    # --- Identity ----------------------------------------------------------
    # WHY String PK (not autoincrement int): the "user identifier" originates
    # in an upstream identity system; we should not invent our own surrogate.
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    # --- Global enabled channels ------------------------------------------
    # JSONB list of ChannelType values, e.g. ["email", "push"].
    # Default = [] (opt-in model: no channels enabled until the user opts in).
    enabled_channels: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )

    # --- Per-type channel preferences -------------------------------------
    # JSONB dict, e.g. {"marketing": ["email"], "alerts": ["email","sms","push"]}
    per_type_preferences: Mapped[dict[str, list[str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )

    # --- Quiet hours -------------------------------------------------------
    # Stored as two `time` columns plus an IANA timezone string.
    # WHY split fields (vs JSON): native types let us index / range-query.
    # Both NULL = "no quiet hours configured".
    quiet_hours_start: Mapped[Optional[time]] = mapped_column(
        Time(timezone=False),
        nullable=True,
    )
    quiet_hours_end: Mapped[Optional[time]] = mapped_column(
        Time(timezone=False),
        nullable=True,
    )
    quiet_hours_timezone: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    # --- Frequency caps ----------------------------------------------------
    # JSONB so we can extend with new windows (per_minute, per_week, ...) without
    # a migration. Shape today: {"per_hour": int|null, "per_day": int|null}.
    frequency_caps: Mapped[dict[str, Optional[int]]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )

    # --- Per-channel destination addresses --------------------------------
    # PRD §2.4 requires every Delivery row to carry a channel-specific
    # `recipient_address`. Storing the per-channel addresses on the user
    # profile is the only way to support `recipient_user_id`-only sends
    # without forcing the caller to pass an address that depends on the
    # channel (which they don't know until preference resolution runs).
    email_address: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    device_token: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)

    # --- Webhook URL -------------------------------------------------------
    # Nullable: only required if the user enables the `webhook` channel.
    webhook_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)

    # --- Pause / resume gate (PRD §3.5) -----------------------------------
    is_paused: Mapped[bool] = mapped_column(nullable=False, default=False)

    # --- Optimistic concurrency control -----------------------------------
    # SQLAlchemy auto-increments this on every UPDATE and adds it to the
    # WHERE clause, so a concurrent writer racing on the same row gets a
    # `StaleDataError` (mapped to HTTP 412 by the API layer). Exposed via
    # the `ETag` response header on GET and required as `If-Match` on
    # write operations.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Relationships -----------------------------------------------------
    # 1-to-N: one user → many notifications (back-populated from Notification).
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="recipient_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Wire the OCC column into SQLAlchemy's mapper so it's auto-managed.
    __mapper_args__ = {
        "version_id_col": version_id,
    }

    def __repr__(self) -> str:
        return f"<UserPreferences user_id={self.user_id!r}>"
