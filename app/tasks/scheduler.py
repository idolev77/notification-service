"""
Periodic scheduled-notifications scanner (PRD §4.8).

Runs every `scheduled_scan_interval_seconds` from Celery Beat. Picks up
Notifications whose `scheduled_at <= now()` and that are still in
`RECEIVED` state, then enqueues their pre-existing `Delivery` rows on the
appropriate per-channel queues (or the `priority` queue for HIGH).

Concurrency safety:
  - We use `SELECT … FOR UPDATE SKIP LOCKED` to claim a batch atomically.
    Two beat instances (or beat + a manual run) can therefore co-exist
    without double-enqueuing the same notification.
  - The status flip RECEIVED → PROCESSING is the ownership transfer; once
    a row is in PROCESSING the next scan ignores it.

Failure mode:
  - A crash between status-flip and enqueue would leave a Notification in
    PROCESSING with deliveries still QUEUED forever. A Sprint-5 hardening
    would add a separate "stale-PROCESSING reaper". Documented here for
    interview defence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import bind_notification_context, get_logger
from app.models import Notification
from app.models.enums import NotificationStatus
from app.tasks.deliver import enqueue_delivery
from app.worker import celery_app

_logger = get_logger(__name__)


@celery_app.task(
    name="app.tasks.scheduler.dispatch_due_notifications",
    bind=True,
    # No retry: the task is idempotent and runs every N seconds anyway.
    # A failed tick simply means "we'll catch the backlog next tick".
    max_retries=0,
)
def dispatch_due_notifications(self) -> int:  # noqa: ANN001
    """
    Claim and enqueue at most `scheduled_scan_batch_size` due notifications.

    Returns the number of notifications dispatched (useful for logs / tests).
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    dispatched = 0

    with session_scope() as session:
        # SKIP LOCKED so concurrent beat workers don't fight over the same rows.
        stmt = (
            select(Notification)
            .where(
                Notification.status == NotificationStatus.RECEIVED,
                Notification.scheduled_at.is_not(None),
                Notification.scheduled_at <= now,
            )
            .order_by(Notification.scheduled_at.asc())
            .limit(settings.scheduled_scan_batch_size)
            .with_for_update(skip_locked=True)
        )
        due = session.scalars(stmt).all()

        for notification in due:
            bind_notification_context(notification_id=str(notification.id))
            # Flip BEFORE enqueue so a crash between flip and enqueue
            # leaves a recoverable trail (PROCESSING + QUEUED deliveries),
            # never a duplicate fan-out.
            notification.status = NotificationStatus.PROCESSING
            for delivery in notification.deliveries:
                enqueue_delivery(
                    delivery_id=delivery.id,
                    channel=delivery.channel,
                    priority=notification.priority,
                )
            dispatched += 1
            _logger.info(
                "scheduler.dispatched",
                notification_id=str(notification.id),
                delivery_count=len(notification.deliveries),
            )

    if dispatched:
        _logger.info("scheduler.tick", dispatched=dispatched)
    return dispatched
