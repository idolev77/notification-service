"""
Tracking endpoints — PRD §3.4.

Read-only views into the notification + delivery state machine. No mutation
happens here; that's reserved for the management router (§3.5).

Endpoints:
  - GET /notifications/{id}                   — top-level + per-channel status
  - GET /notifications/{id}/deliveries        — per-channel deliveries only
  - GET /users/{user_id}/notifications        — paginated user history
  - GET /stats/deliveries                     — aggregate counts by channel
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db_session
from app.models import Delivery, Notification
from app.models.enums import DeliveryStatus
from app.schemas.tracking import (
    AggregateStatsResponse,
    ChannelStatsView,
    DeliveryView,
    NotificationStatusView,
    UserDeliveryHistoryItem,
)

router = APIRouter(tags=["tracking"])

# Terminal failure statuses kept in one place so the stats query stays honest.
_FAILED_STATUSES = (DeliveryStatus.FAILED, DeliveryStatus.PERMANENTLY_FAILED)


def _delivery_to_view(delivery: Delivery) -> DeliveryView:
    return DeliveryView(
        id=delivery.id,
        channel=delivery.channel,
        recipient_address=delivery.recipient_address,
        status=delivery.status,
        attempts=delivery.attempts,
        last_attempt_at=delivery.last_attempt_at,
        delivered_at=delivery.delivered_at,
        error_message=delivery.error_message,
        provider_response=delivery.provider_response,
    )


@router.get(
    "/notifications/{notification_id}",
    response_model=NotificationStatusView,
    summary="Get top-level notification status with per-channel deliveries.",
)
def get_notification_status(
    notification_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db_session),
) -> NotificationStatusView:
    """
    Returns the parent Notification + every Delivery row in a single response.

    `selectinload` issues one extra query for deliveries (avoids N+1) and
    keeps the response shape stable regardless of fan-out size.
    """
    stmt = (
        select(Notification)
        .where(Notification.id == notification_id)
        .options(selectinload(Notification.deliveries))
    )
    notification = db.execute(stmt).scalar_one_or_none()
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification {notification_id} not found.",
        )

    return NotificationStatusView(
        id=notification.id,
        notification_type=notification.notification_type,
        recipient_user_id=notification.recipient_user_id,
        recipient_contact=notification.recipient_contact,
        priority=notification.priority,
        status=notification.status,
        scheduled_at=notification.scheduled_at,
        created_at=notification.created_at,
        updated_at=notification.updated_at,
        deliveries=[_delivery_to_view(d) for d in notification.deliveries],
    )


@router.get(
    "/notifications/{notification_id}/deliveries",
    response_model=list[DeliveryView],
    summary="Get per-channel delivery rows for a notification.",
)
def list_notification_deliveries(
    notification_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db_session),
) -> list[DeliveryView]:
    """Per-channel breakdown only — useful for tight polling loops."""
    # Cheap existence check so callers get a real 404 rather than [].
    if db.get(Notification, notification_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification {notification_id} not found.",
        )

    rows = db.execute(
        select(Delivery)
        .where(Delivery.notification_id == notification_id)
        .order_by(Delivery.channel.asc())
    ).scalars().all()
    return [_delivery_to_view(d) for d in rows]


@router.get(
    "/users/{user_id}/notifications",
    response_model=list[UserDeliveryHistoryItem],
    summary="Paginated delivery history for a user (newest first).",
)
def get_user_delivery_history(
    user_id: str = Path(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
) -> list[UserDeliveryHistoryItem]:
    """
    Returns one row per Delivery (joined with its parent Notification)
    so the consumer sees per-channel granularity without a second call.

    Ordered by Notification.created_at DESC then by channel for stable
    pagination under cursor-style consumers.
    """
    stmt = (
        select(Notification, Delivery)
        .join(Delivery, Delivery.notification_id == Notification.id)
        .where(Notification.recipient_user_id == user_id)
        .order_by(Notification.created_at.desc(), Delivery.channel.asc())
        .limit(limit)
        .offset(offset)
    )
    return [
        UserDeliveryHistoryItem(
            notification_id=notif.id,
            notification_type=notif.notification_type,
            notification_status=notif.status,
            delivery_id=delivery.id,
            channel=delivery.channel,
            delivery_status=delivery.status,
            attempts=delivery.attempts,
            delivered_at=delivery.delivered_at,
            created_at=notif.created_at,
        )
        for notif, delivery in db.execute(stmt).all()
    ]


@router.get(
    "/stats/deliveries",
    response_model=AggregateStatsResponse,
    summary="Aggregate delivery counts grouped by channel.",
)
def get_delivery_stats(
    since: datetime | None = Query(
        None,
        description="Inclusive lower bound on Notification.created_at (UTC).",
    ),
    until: datetime | None = Query(
        None,
        description="Exclusive upper bound on Notification.created_at (UTC).",
    ),
    db: Session = Depends(get_db_session),
) -> AggregateStatsResponse:
    """
    Returns per-channel sent / delivered / failed counts.

    Definitions:
      - `sent`      = deliveries with `attempts > 0` (left the queue at least once)
      - `delivered` = deliveries in DELIVERED status
      - `failed`    = deliveries in FAILED or PERMANENTLY_FAILED status

    Window filter is optional; if omitted, returns all-time counts. The
    filter applies to `Notification.created_at` so a single query window
    is consistent across both totals and the channel breakdown.
    """
    if since is not None and until is not None and since >= until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`since` must be strictly less than `until`.",
        )
    notification_filter = []
    if since is not None:
        notification_filter.append(Notification.created_at >= since)
    if until is not None:
        notification_filter.append(Notification.created_at < until)

    # Single grouped query for the per-channel breakdown.
    # `case(...)` returns 1/0 per row so SUM produces the bucket count without
    # needing dialect-specific BOOLEAN-to-INT casts.
    breakdown_stmt = (
        select(
            Delivery.channel,
            func.count().label("total"),
            func.coalesce(
                func.sum(case((Delivery.attempts > 0, 1), else_=0)), 0
            ).label("sent"),
            func.coalesce(
                func.sum(
                    case((Delivery.status == DeliveryStatus.DELIVERED, 1), else_=0)
                ),
                0,
            ).label("delivered"),
            func.coalesce(
                func.sum(
                    case((Delivery.status.in_(_FAILED_STATUSES), 1), else_=0)
                ),
                0,
            ).label("failed"),
        )
        .join(Notification, Notification.id == Delivery.notification_id)
        .where(*notification_filter)
        .group_by(Delivery.channel)
    )
    by_channel: list[ChannelStatsView] = []
    total_deliveries = 0
    for row in db.execute(breakdown_stmt).all():
        total_deliveries += int(row.total or 0)
        by_channel.append(
            ChannelStatsView(
                channel=row.channel,
                sent=int(row.sent or 0),
                delivered=int(row.delivered or 0),
                failed=int(row.failed or 0),
            )
        )

    total_notifications = db.execute(
        select(func.count()).select_from(Notification).where(*notification_filter)
    ).scalar_one()

    return AggregateStatsResponse(
        window_start=since,
        window_end=until,
        total_notifications=int(total_notifications),
        total_deliveries=total_deliveries,
        by_channel=by_channel,
    )
