"""Pydantic schemas for tracking endpoints (PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import (
    ChannelType,
    DeliveryStatus,
    NotificationPriority,
    NotificationStatus,
)


class DeliveryView(BaseModel):
    id: uuid.UUID
    channel: ChannelType
    recipient_address: str
    status: DeliveryStatus
    attempts: int
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    error_message: str | None
    provider_response: dict | None


class NotificationStatusView(BaseModel):
    """Top-level notification status + per-channel deliveries (§3.4)."""

    id: uuid.UUID
    notification_type: str
    recipient_user_id: str | None
    recipient_contact: str | None
    priority: NotificationPriority
    status: NotificationStatus
    scheduled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deliveries: list[DeliveryView]


class UserDeliveryHistoryItem(BaseModel):
    """Flat row for the user-history endpoint (Notification ⨝ Delivery)."""

    notification_id: uuid.UUID
    notification_type: str
    notification_status: NotificationStatus
    delivery_id: uuid.UUID
    channel: ChannelType
    delivery_status: DeliveryStatus
    attempts: int
    delivered_at: datetime | None
    created_at: datetime


class ChannelStatsView(BaseModel):
    """One row per (channel, terminal_status) bucket."""

    channel: ChannelType
    sent: int          # attempts > 0 (everything that left the queue at least once)
    delivered: int     # status = DELIVERED
    failed: int        # status in (FAILED, PERMANENTLY_FAILED)


class AggregateStatsResponse(BaseModel):
    window_start: datetime | None
    window_end: datetime | None
    total_notifications: int
    total_deliveries: int
    by_channel: list[ChannelStatsView]
