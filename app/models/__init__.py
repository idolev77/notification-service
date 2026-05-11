"""
Public model surface.

WHY a single re-export module:
  Downstream code (repositories, Alembic env.py, tests) imports from
  `app.models` only. Internal file moves never break callers.
"""

from app.models.base import Base, TimestampMixin
from app.models.delivery import Delivery
from app.models.enums import (
    ChannelType,
    DeliveryStatus,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification import Notification
from app.models.template import Template
from app.models.user_preferences import UserPreferences

__all__ = [
    "Base",
    "TimestampMixin",
    "ChannelType",
    "DeliveryStatus",
    "NotificationPriority",
    "NotificationStatus",
    "UserPreferences",
    "Template",
    "Notification",
    "Delivery",
]
