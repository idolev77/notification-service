"""
Management endpoints — PRD §3.5 (the parts not already covered elsewhere).

Already covered:
  - Cancel scheduled notification → see `app/api/notifications.py`
  - Pause / resume user            → handled by `PUT /preferences/{user_id}`
                                     (toggle `is_paused`)

This router adds:
  - POST /notifications/{id}/resend   — re-enqueue terminally-failed deliveries
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.core.logging import bind_notification_context, get_logger
from app.models import Delivery, Notification, UserPreferences
from app.models.enums import DeliveryStatus, NotificationStatus
from app.schemas.preferences import UserPreferencesResponse
from app.schemas.tracking import DeliveryView
from app.tasks.deliver import enqueue_delivery

router = APIRouter(tags=["management"])

_logger = get_logger(__name__)

# Statuses we consider "resendable". PERMANENTLY_FAILED is the primary case
# (Celery exhausted retries); FAILED is included so an operator can also
# nudge a delivery that crash-looped between attempts.
_RESENDABLE_STATUSES = (DeliveryStatus.FAILED, DeliveryStatus.PERMANENTLY_FAILED)


@router.post(
    "/notifications/{notification_id}/resend",
    response_model=list[DeliveryView],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-enqueue failed deliveries for a notification (PRD §3.5).",
)
def resend_failed_deliveries(
    notification_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db_session),
) -> list[DeliveryView]:
    """
    Reset every FAILED / PERMANENTLY_FAILED delivery row back to QUEUED and
    re-enqueue it on its channel queue.

    Behaviour & guarantees:
      - 404 if the notification does not exist.
      - 409 if the notification is CANCELLED (operator must un-cancel first
        by issuing a fresh request — we will not silently override it).
      - If there are no failed deliveries, returns `[]` with 202; the call
        is idempotent in that sense.
      - The retry counter (`attempts`) is NOT reset — the operator opt-in
        is to grant additional attempts beyond `max_retries`, not to hide
        history. The fresh enqueue starts a new attempt cycle from the
        worker's perspective.
      - Parent `Notification.status` flips back to PROCESSING so aggregate
        status reflects the in-flight retry.
    """
    # Acquire a row-level lock on the parent Notification FIRST. Without this,
    # two concurrent /resend calls each see the same FAILED deliveries, both
    # flip them to QUEUED, and both call enqueue_delivery -> double dispatch.
    # Locking the parent serializes the whole resend operation per-notification.
    notification = db.execute(
        select(Notification)
        .where(Notification.id == notification_id)
        .with_for_update()
    ).scalar_one_or_none()
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification {notification_id} not found.",
        )
    bind_notification_context(notification_id=str(notification_id))
    if notification.status is NotificationStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot resend a cancelled notification.",
        )

    failed_deliveries = db.execute(
        select(Delivery)
        .where(
            Delivery.notification_id == notification_id,
            Delivery.status.in_(_RESENDABLE_STATUSES),
        )
        .with_for_update()  # belt-and-braces: also lock the child rows
    ).scalars().all()

    if not failed_deliveries:
        return []

    for delivery in failed_deliveries:
        # Reset terminal markers so the dispatcher's invariants hold:
        # only QUEUED rows get claimed for SENDING.
        delivery.status = DeliveryStatus.QUEUED
        delivery.error_message = None

    # Reflect aggregate state at the parent level so trackers see "in flight".
    notification.status = NotificationStatus.PROCESSING

    db.commit()

    # Refresh + enqueue AFTER commit so workers cannot pick up a row that
    # the API transaction hasn't yet persisted.
    for delivery in failed_deliveries:
        db.refresh(delivery)
        enqueue_delivery(
            delivery_id=delivery.id,
            channel=delivery.channel,
            priority=notification.priority,
        )
        _logger.info(
            "management.resend.enqueued",
            notification_id=str(notification.id),
            delivery_id=str(delivery.id),
            channel=delivery.channel.value,
            attempts_so_far=delivery.attempts,
        )

    return [
        DeliveryView(
            id=d.id,
            channel=d.channel,
            recipient_address=d.recipient_address,
            status=d.status,
            attempts=d.attempts,
            last_attempt_at=d.last_attempt_at,
            delivered_at=d.delivered_at,
            error_message=d.error_message,
            provider_response=d.provider_response,
        )
        for d in failed_deliveries
    ]


# ---------------------------------------------------------------------------
# Pause / resume (PRD §3.5)
# ---------------------------------------------------------------------------
# These are dedicated routes (instead of a general PUT /preferences toggle)
# so the operation is discoverable in the OpenAPI schema and unambiguous in
# server logs. They mutate ONLY `is_paused` — every other preference field
# is left untouched (idempotent).

_USER_ID_PATH = Path(..., min_length=1, max_length=128)


def _set_user_paused(
    *, db: Session, user_id: str, paused: bool
) -> UserPreferences:
    prefs = db.get(UserPreferences, user_id)
    if prefs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No preferences exist for user {user_id!r}.",
        )
    prefs.is_paused = paused
    db.commit()
    db.refresh(prefs)
    _logger.info(
        "management.user_pause_state_changed",
        user_id=user_id,
        is_paused=paused,
    )
    return prefs


def _prefs_to_response(prefs: UserPreferences) -> UserPreferencesResponse:
    """Local serializer; keeps this router decoupled from the prefs router."""
    from app.models.enums import ChannelType  # local import: avoids top-level cycle
    return UserPreferencesResponse(
        user_id=prefs.user_id,
        enabled_channels=[ChannelType(c) for c in prefs.enabled_channels],
        per_type_preferences={
            ntype: [ChannelType(c) for c in channels]
            for ntype, channels in prefs.per_type_preferences.items()
        },
        quiet_hours_start=prefs.quiet_hours_start,
        quiet_hours_end=prefs.quiet_hours_end,
        quiet_hours_timezone=prefs.quiet_hours_timezone,
        frequency_caps=prefs.frequency_caps,
        webhook_url=prefs.webhook_url,
        email_address=prefs.email_address,
        phone_number=prefs.phone_number,
        device_token=prefs.device_token,
        is_paused=prefs.is_paused,
    )


@router.post(
    "/users/{user_id}/pause",
    response_model=UserPreferencesResponse,
    status_code=status.HTTP_200_OK,
    summary="Pause notifications for a user (PRD §3.5).",
)
def pause_user(
    user_id: str = _USER_ID_PATH,
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    return _prefs_to_response(_set_user_paused(db=db, user_id=user_id, paused=True))


@router.post(
    "/users/{user_id}/resume",
    response_model=UserPreferencesResponse,
    status_code=status.HTTP_200_OK,
    summary="Resume notifications for a user (PRD §3.5).",
)
def resume_user(
    user_id: str = _USER_ID_PATH,
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    return _prefs_to_response(_set_user_paused(db=db, user_id=user_id, paused=False))
