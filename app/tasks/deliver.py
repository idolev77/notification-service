"""
Celery delivery tasks — one per channel queue.

State machine (PRD §4.1) executed per task invocation:

    Delivery.status:  QUEUED  →  SENDING  →  DELIVERED          (success)
                              →  SENDING  →  FAILED → (retry)   (transient)
                              →  SENDING  →  PERMANENTLY_FAILED (terminal)

Notification.status is recomputed by the dispatcher after every attempt
based on the aggregate state of its sibling deliveries.

WHY `autoretry_for=(RetryableProviderError,)`:
  - Celery handles the requeue + backoff for us — no hand-rolled scheduler.
  - `NonRetryableProviderError` is NOT in the list, so Celery propagates it
    and we transition to PERMANENTLY_FAILED in the `on_failure` hook below.
"""

from __future__ import annotations

import uuid

import structlog
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.channels.base import NonRetryableProviderError, RetryableProviderError
from app.core.config import get_settings
from app.core.logging import (
    CELERY_REQUEST_ID_HEADER,
    REQUEST_ID_LOG_KEY,
    bind_notification_context,
    clear_notification_context,
    get_logger,
)
from app.models.enums import ChannelType, NotificationPriority
from app.services.dispatcher import (
    execute_delivery_attempt,
    mark_delivery_permanently_failed,
)
from app.worker import celery_app

_logger = get_logger(__name__)
_settings = get_settings()


class _DeliveryTask(Task):
    """
    Custom base task that converts terminal failures into PERMANENTLY_FAILED.

    Triggered when:
      - A NonRetryableProviderError propagates (immediate terminal fail).
      - Celery exhausts `max_retries` for retryable errors.
    """

    autoretry_for = (RetryableProviderError, SoftTimeLimitExceeded)
    retry_backoff = True              # exponential: base, base*2, base*4, ...
    retry_backoff_max = 600           # cap one wait at 10 minutes
    retry_jitter = True               # avoid thundering-herd retries
    max_retries = _settings.max_retry_attempts

    # Acks_late is set globally; restated here for documentation visibility.
    acks_late = True

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:  # noqa: D401, ANN001
        """
        Called by Celery exactly once when the task gives up (no more retries).

        At this point:
          - Either `exc` is `NonRetryableProviderError`, or
          - We exhausted `max_retries` on a `RetryableProviderError`.
        Either way the Delivery transitions to PERMANENTLY_FAILED.
        """
        delivery_id = kwargs.get("delivery_id") or (args[0] if args else None)
        if delivery_id is None:
            _logger.error("delivery.on_failure.missing_delivery_id", error=str(exc))
            return
        try:
            mark_delivery_permanently_failed(
                delivery_id=uuid.UUID(str(delivery_id)),
                error_message=str(exc),
            )
        except Exception:  # noqa: BLE001
            # Never raise from on_failure — it would mask the original error.
            _logger.exception(
                "delivery.on_failure.persist_failed",
                delivery_id=str(delivery_id),
            )


def _run_delivery(delivery_id: str, channel: ChannelType) -> None:
    """
    Shared body for every channel task.

    Kept as a free function (not a method) so the per-channel task wrappers
    are trivial and the unit-of-work logic is testable without Celery.
    """
    parsed_id = uuid.UUID(delivery_id)
    bind_notification_context(delivery_id=str(parsed_id), channel=channel.value)
    try:
        # Provider exceptions intentionally propagate so Celery's autoretry
        # machinery and our `on_failure` hook see them.
        execute_delivery_attempt(delivery_id=parsed_id, channel=channel)
    finally:
        clear_notification_context()


# ---------------------------------------------------------------------------
# One task per channel. Bodies are 1-liners — all logic lives in dispatcher.
# ---------------------------------------------------------------------------

@celery_app.task(
    base=_DeliveryTask,
    name="app.tasks.deliver.deliver_email",
    bind=True,
)
def deliver_email(self, delivery_id: str) -> None:  # noqa: ANN001
    """Process a single email Delivery row."""
    _run_delivery(delivery_id, ChannelType.EMAIL)


@celery_app.task(
    base=_DeliveryTask,
    name="app.tasks.deliver.deliver_sms",
    bind=True,
)
def deliver_sms(self, delivery_id: str) -> None:  # noqa: ANN001
    """Process a single SMS Delivery row. Provider impl arrives in Sprint 3."""
    _run_delivery(delivery_id, ChannelType.SMS)


@celery_app.task(
    base=_DeliveryTask,
    name="app.tasks.deliver.deliver_push",
    bind=True,
)
def deliver_push(self, delivery_id: str) -> None:  # noqa: ANN001
    """Process a single push Delivery row. Provider impl arrives in Sprint 3."""
    _run_delivery(delivery_id, ChannelType.PUSH)


@celery_app.task(
    base=_DeliveryTask,
    name="app.tasks.deliver.deliver_webhook",
    bind=True,
)
def deliver_webhook(self, delivery_id: str) -> None:  # noqa: ANN001
    """Process a single webhook Delivery row. Provider impl arrives in Sprint 3."""
    _run_delivery(delivery_id, ChannelType.WEBHOOK)


# ---------------------------------------------------------------------------
# Public dispatch helper used by the API service layer.
# ---------------------------------------------------------------------------

# Map ChannelType → registered Celery task callable. Adding a channel:
# add the task above and register it here. The dispatcher never imports
# Celery directly.
DELIVERY_TASKS: dict[ChannelType, Task] = {
    ChannelType.EMAIL:   deliver_email,
    ChannelType.SMS:     deliver_sms,
    ChannelType.PUSH:    deliver_push,
    ChannelType.WEBHOOK: deliver_webhook,
}


def enqueue_delivery(
    *,
    delivery_id: uuid.UUID,
    channel: ChannelType,
    priority: NotificationPriority = NotificationPriority.NORMAL,
) -> None:
    """
    Enqueue a Delivery onto its channel-specific queue.

    Routing rule (PRD §6 Sprint-4 nice-to-have "priority queues"):
      - HIGH-priority deliveries are routed to the dedicated `priority`
        queue, which every worker drains AHEAD of the per-channel queues
        (see worker `-Q priority,email,...` order in docker-compose.yml).
      - NORMAL/LOW deliveries flow to the per-channel queue as configured
        in `worker.py task_routes`.

    Correlation: if a `request_id` is bound in the caller's structlog
    contextvars (set by the FastAPI request middleware), it is propagated
    as a Celery task header so the worker re-binds it before executing the
    task. End-to-end correlation works for both immediate API-driven sends
    and beat-scheduler-driven sends.

    Centralised so the service layer is decoupled from Celery internals.
    """
    task = DELIVERY_TASKS[channel]
    headers = _propagation_headers()
    if priority is NotificationPriority.HIGH:
        task.apply_async(
            args=[str(delivery_id)], queue="priority", headers=headers
        )
    else:
        task.apply_async(args=[str(delivery_id)], headers=headers)


def _propagation_headers() -> dict[str, str]:
    """Pull correlation IDs out of the current structlog context (if any)."""
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get(REQUEST_ID_LOG_KEY)
    if request_id:
        return {CELERY_REQUEST_ID_HEADER: str(request_id)}
    return {}
