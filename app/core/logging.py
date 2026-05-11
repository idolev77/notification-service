"""
Structured logging setup (structlog).

WHY structlog over stdlib logging alone:
  - Native key/value context binding — we can attach
    `notification_id`, `delivery_id`, `channel`, `attempt` etc. to every
    log line without string interpolation.
  - JSON output is one renderer swap away → log aggregators (ELK, Loki,
    CloudWatch) can index fields directly.
  - PRD §1.3: "Structured logging with notification context" is satisfied
    by `bind_notification_context()` below.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import get_settings


def configure_logging() -> None:
    """
    Idempotent global logging configuration.

    Call once on application startup (FastAPI lifespan + Celery worker init).
    Subsequent calls are no-ops thanks to structlog's own guard.
    """
    settings = get_settings()
    log_level_int = logging.getLevelName(settings.log_level)

    # Route stdlib logs (uvicorn, sqlalchemy, celery) through the same sink
    # so the operator gets one consistent stream.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level_int,
    )

    # Renderer choice: JSON in prod-like envs; pretty console for local dev.
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # picks up bound context
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a module-scoped structlog logger."""
    return structlog.get_logger(name)


def bind_notification_context(**fields: Any) -> None:
    """
    Bind notification-scoped fields into the current async/thread context.

    Typical usage at the top of a request handler or Celery task:

        bind_notification_context(
            notification_id=str(notification.id),
            channel="email",
            attempt=1,
        )

    Every subsequent log call in the same context inherits these fields,
    giving us the "structured logging with notification context" required
    by PRD §1.3 without threading dicts through every function call.
    """
    structlog.contextvars.bind_contextvars(**fields)


def clear_notification_context() -> None:
    """Clear all bound context. Call at task/request boundaries."""
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Correlation ID propagation
# ---------------------------------------------------------------------------
# The structured-log key used everywhere (HTTP middleware, Celery signals,
# downstream provider logs) so a single grep on `request_id=` traces a full
# request through API -> broker -> worker -> provider.
REQUEST_ID_LOG_KEY = "request_id"

# HTTP header name (in/out) and Celery task header name.
REQUEST_ID_HEADER = "X-Request-Id"
CELERY_REQUEST_ID_HEADER = "x_request_id"


def bind_request_id(request_id: str) -> None:
    """Bind the inbound/propagated correlation ID into the current context."""
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_LOG_KEY: request_id})
