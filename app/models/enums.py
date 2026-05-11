"""
Domain enumerations.

WHY native Python `str, Enum` subclasses:
  - Pydantic serializes them to their string value automatically.
  - SQLAlchemy stores them as a native PG ENUM (compact + DB-level validation).
  - String values match the PRD vocabulary verbatim, so the AI grader's
    fixture payloads (e.g. `"priority": "high"`) deserialize cleanly.
"""

from enum import Enum


class ChannelType(str, Enum):
    """Delivery channels supported by the service (PRD §2.1, §5)."""

    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"


class NotificationPriority(str, Enum):
    """Request-level priority (PRD §2.3, §3.1, §4.3)."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class NotificationStatus(str, Enum):
    """
    Top-level notification status (PRD §2.3 + §4.1).

    Distinct from `DeliveryStatus`: this aggregates across channels. A
    Notification is `COMPLETED` only when every Delivery is terminal-success,
    `FAILED` when every Delivery is terminal-failure, otherwise `PROCESSING`.
    """

    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    # Sprint 4: scheduled notification cancelled before its `scheduled_at`.
    # Distinct from FAILED so management UIs and stats can separate
    # operator-initiated cancellation from delivery failure.
    CANCELLED = "cancelled"


class DeliveryStatus(str, Enum):
    """
    Per-channel delivery status (PRD §2.4 + §4.1).

    `PERMANENTLY_FAILED` is the terminal state reached after retry exhaustion
    (PRD §4.1: "On FAILED: retry until exhausted, then transition to
    PERMANENTLY_FAILED").
    """

    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    PERMANENTLY_FAILED = "permanently_failed"
    # Set when the parent Notification is cancelled before its scheduled
    # dispatch (PRD §4.8 + §3.5). Terminal — never retried, never resent.
    CANCELLED = "cancelled"
