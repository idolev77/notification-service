"""
Celery application + per-channel queue routing.

WHY one queue per channel (§4.1, §4.4):
  - Failure isolation — a stuck SMS task can't block email throughput.
  - Independent scaling — workers can subscribe to a subset of queues.
  - Clean priority story (Sprint 4) — priority is a queue-level concept.

WHY `task_acks_late=True` + `task_reject_on_worker_lost=True`:
  - Guarantees at-least-once delivery semantics for delivery tasks.
  - If a worker is killed mid-attempt the task is requeued, not lost.
  - Provider calls are idempotent at the DB level (we update by Delivery.id),
    so re-execution of the same attempt is safe.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import task_postrun, task_prerun
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import (
    CELERY_REQUEST_ID_HEADER,
    bind_request_id,
    clear_notification_context,
    configure_logging,
)

# Initialize structured logging once when this module is imported by the
# Celery process (worker bootstrap / beat bootstrap / API bootstrap).
configure_logging()

_settings = get_settings()

celery_app = Celery(
    "notification_service",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
    # Auto-discover tasks under `app.tasks` so the worker registers them on boot.
    include=["app.tasks.deliver", "app.tasks.scheduler"],
)

# ---------------------------------------------------------------------------
# Routing — one queue per channel + a `priority` queue (PRD §4.6 nice-to-have).
# Queue names match the `-Q` list in docker-compose.yml's worker command.
# Priority queue is also drained by every channel worker; the scheduler/
# enqueuer routes high-priority deliveries there so they jump ahead of
# normal traffic without disturbing per-channel isolation.
# ---------------------------------------------------------------------------
celery_app.conf.task_queues = (
    Queue("email"),
    Queue("sms"),
    Queue("push"),
    Queue("webhook"),
    Queue("priority"),
    Queue("scheduler"),
    Queue("default"),
)

celery_app.conf.task_default_queue = "default"

# Static routing: each per-channel task is bound to its dedicated queue.
# (Per-call routing in `enqueue_delivery` overrides this for HIGH priority.)
celery_app.conf.task_routes = {
    "app.tasks.deliver.deliver_email":   {"queue": "email"},
    "app.tasks.deliver.deliver_sms":     {"queue": "sms"},
    "app.tasks.deliver.deliver_push":    {"queue": "push"},
    "app.tasks.deliver.deliver_webhook": {"queue": "webhook"},
    "app.tasks.scheduler.dispatch_due_notifications": {"queue": "scheduler"},
}

# ---------------------------------------------------------------------------
# Reliability defaults (apply to every task unless overridden).
# ---------------------------------------------------------------------------
celery_app.conf.update(
    # Always serialize as JSON — keeps payloads inspectable in Redis and
    # avoids pickle's security footguns.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # At-least-once delivery semantics (see module docstring).
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Don't fetch more than one task per worker process at a time — keeps
    # long-running provider calls from starving siblings.
    worker_prefetch_multiplier=1,

    # Hard ceilings on task wall-time so a hung provider (TLS handshake
    # stuck, infinite redirect, dead socket) cannot wedge a worker forever.
    #   soft_time_limit -> raises SoftTimeLimitExceeded inside the task,
    #                      letting cleanup code run; we treat it as a
    #                      transient failure (autoretry kicks in).
    #   time_limit      -> hard SIGKILL of the worker child after this many
    #                      seconds. Must be > soft so the soft handler has a
    #                      chance to record the failure first.
    # Values chosen to comfortably exceed the slowest legitimate provider
    # call (webhook timeout is 5s by default) while still bounding damage.
    task_soft_time_limit=_settings.task_soft_time_limit_seconds,
    task_time_limit=_settings.task_hard_time_limit_seconds,

    # UTC everywhere — matches our DB timestamps (timezone-aware UTC).
    enable_utc=True,
    timezone="UTC",
)


# ---------------------------------------------------------------------------
# Beat schedule — periodic scheduled-notifications scanner (PRD §4.8).
# The cadence is config-driven so ops can tune it without a code change.
# ---------------------------------------------------------------------------
celery_app.conf.beat_schedule = {
    "dispatch-due-notifications": {
        "task": "app.tasks.scheduler.dispatch_due_notifications",
        "schedule": float(_settings.scheduled_scan_interval_seconds),
        "options": {"queue": "scheduler"},
    },
}


# ---------------------------------------------------------------------------
# Correlation ID + context lifecycle for every task.
#
# `task_prerun`:  Pull `x_request_id` out of the task headers (set by the
#                 enqueuer) and bind it to structlog so every log line in
#                 the task body carries the same `request_id` as the
#                 originating HTTP request.
# `task_postrun`: Clear ALL bound contextvars so the next task on the same
#                 worker process inherits a clean context — same defence
#                 we apply at the FastAPI middleware boundary.
# ---------------------------------------------------------------------------

@task_prerun.connect
def _bind_task_context(sender=None, task_id=None, task=None, **_):  # noqa: ANN001
    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    request_id = headers.get(CELERY_REQUEST_ID_HEADER)
    if request_id:
        bind_request_id(str(request_id))


@task_postrun.connect
def _clear_task_context(**_):  # noqa: ANN001
    clear_notification_context()
