"""
`POST /notifications` route — accepts every field listed in PRD §3.1.

The route is intentionally thin:
  1. Pydantic validates the payload.
  2. Service layer creates rows + enqueues tasks.
  3. We commit and return a 202 with the persisted Notification.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.core.logging import bind_notification_context, get_logger
from app.models import Notification
from app.models.enums import DeliveryStatus, NotificationStatus
from app.schemas.notifications import (
    BatchSendItemResult,
    BatchSendRequest,
    BatchSendResponse,
    SendNotificationRequest,
    SendNotificationResponse,
)
from app.services.notifications import create_and_dispatch_notification

router = APIRouter(prefix="/notifications", tags=["notifications"])

_logger = get_logger(__name__)


@router.post(
    "",
    response_model=SendNotificationResponse,
    # 202: the API has accepted the work; per-channel delivery is async.
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a notification (async per-channel delivery).",
)
def send_notification(
    request: SendNotificationRequest,
    db: Session = Depends(get_db_session),
) -> SendNotificationResponse:
    """
    Accept a notification request and hand it to the dispatcher.

    Returns immediately with the persisted Notification's id and current
    status. Clients poll the tracking endpoints (Sprint 5) for progress.
    """
    try:
        notification = create_and_dispatch_notification(session=db, request=request)
        db.commit()
    except ValueError as exc:
        # Domain-level validation errors map to 422.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError:
        # Persistence failure — log and surface a 500 without leaking internals.
        db.rollback()
        _logger.exception("notification.persist_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist notification.",
        )

    return SendNotificationResponse(
        id=notification.id,
        status=notification.status,
        scheduled_at=notification.scheduled_at,
        created_at=notification.created_at,
    )


def _merge_variables(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    """Shallow merge: per-recipient `variables` win over batch-level `variables`."""
    if not override:
        return base
    merged = dict(base)
    merged.update(override)
    return merged


@router.post(
    "/batch",
    response_model=BatchSendResponse,
    status_code=status.HTTP_207_MULTI_STATUS,
    summary="Send the same notification to multiple recipients (PRD §1.4).",
)
def send_notification_batch(
    request: BatchSendRequest,
    db: Session = Depends(get_db_session),
) -> BatchSendResponse:
    """
    Fan-out one notification body to N recipients.

    Failure isolation: each recipient is processed in its own savepoint so a
    bad recipient (e.g. unknown user, invalid contact) does NOT poison the
    whole batch. Returns 207 Multi-Status with per-recipient outcome.
    """
    results: list[BatchSendItemResult] = []
    accepted = 0
    failed = 0

    for index, recipient in enumerate(request.recipients):
        item = BatchSendItemResult(
            index=index,
            recipient_user_id=recipient.recipient_user_id,
            recipient_contact=recipient.recipient_contact,
        )
        try:
            single_request = SendNotificationRequest(
                recipient_user_id=recipient.recipient_user_id,
                recipient_contact=recipient.recipient_contact,
                notification_type=request.notification_type,
                content=request.content,
                variables=_merge_variables(request.variables, recipient.variables),
                priority=request.priority,
                channels_override=request.channels_override,
                scheduled_at=request.scheduled_at,
            )
            # Per-recipient savepoint: a single failure rolls back ONLY that
            # recipient's writes, leaving prior successful inserts intact.
            with db.begin_nested():
                notification = create_and_dispatch_notification(
                    session=db, request=single_request
                )
            item.notification_id = notification.id
            item.status = notification.status
            accepted += 1
        except ValueError as exc:
            item.error = str(exc)
            failed += 1
        except SQLAlchemyError:
            _logger.exception(
                "notification.batch.persist_failed", recipient_index=index
            )
            item.error = "Failed to persist notification."
            failed += 1
        results.append(item)

    if accepted > 0:
        db.commit()
    else:
        # Nothing committed; release the transaction so the next request is clean.
        db.rollback()

    _logger.info(
        "notification.batch.completed",
        total=len(request.recipients),
        accepted=accepted,
        failed=failed,
    )
    return BatchSendResponse(accepted=accepted, failed=failed, results=results)


@router.post(
    "/{notification_id}/cancel",
    response_model=SendNotificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel a scheduled notification before it dispatches (PRD §3.5).",
)
def cancel_scheduled_notification(
    notification_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db_session),
) -> SendNotificationResponse:
    """
    Cancel a notification that is still in the RECEIVED + scheduled state.

    Rules:
      - Only `RECEIVED` notifications with a `scheduled_at` can be cancelled.
        Anything PROCESSING/COMPLETED/FAILED is past the point of no return
        — workers may already be in flight — and we refuse with 409.
      - Cascade: marking the parent CANCELLED is sufficient; the scheduler
        only picks up RECEIVED rows so the queued Deliveries are skipped.
    """
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification {notification_id} not found.",
        )
    if notification.scheduled_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only scheduled notifications can be cancelled.",
        )
    if notification.status is not NotificationStatus.RECEIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot cancel a notification in status "
                f"{notification.status.value!r}; only RECEIVED is cancellable."
            ),
        )

    bind_notification_context(notification_id=str(notification.id))
    notification.status = NotificationStatus.CANCELLED
    # Cascade: any pre-created Delivery rows are still QUEUED waiting for the
    # scheduler to fan them out. Mark them CANCELLED so they never get picked
    # up and so tracking endpoints surface the terminal state honestly.
    for delivery in notification.deliveries:
        if delivery.status is DeliveryStatus.QUEUED:
            delivery.status = DeliveryStatus.CANCELLED
    db.commit()
    db.refresh(notification)
    _logger.info("notification.cancelled", notification_id=str(notification.id))

    return SendNotificationResponse(
        id=notification.id,
        status=notification.status,
        scheduled_at=notification.scheduled_at,
        created_at=notification.created_at,
    )
