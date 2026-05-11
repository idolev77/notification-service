"""
FastAPI application entrypoint.

Boot order:
  1. Configure structured logging (must run before any provider/task import).
  2. Eagerly import `app.channels` so every `@register_provider` decorator
     fires and the registry is populated before the first request.
  3. Mount routers.

WHY no DB-init / migration logic here:
  - Migrations run via `alembic upgrade head` from the api container's
    start command (see docker-compose.yml). The API process never
    creates tables itself — that's the migration tool's job.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.management import router as management_router
from app.api.notifications import router as notifications_router
from app.api.preferences import router as preferences_router
from app.api.templates import router as templates_router
from app.api.tracking import router as tracking_router
from app.core.db import get_engine
from app.core.logging import (
    REQUEST_ID_HEADER,
    bind_request_id,
    clear_notification_context,
    configure_logging,
    get_logger,
)

# Side-effect import: registers every concrete ChannelProvider.
import app.channels  # noqa: F401

configure_logging()
_logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _logger.info("api.startup")
    try:
        yield
    finally:
        # Drain the SQLAlchemy connection pool so a rolling deploy does not
        # leak Postgres connections (each old worker process otherwise holds
        # `pool_size` open conns until the OS reaps them).
        try:
            get_engine().dispose()
            _logger.info("api.shutdown.engine_disposed")
        except Exception:  # noqa: BLE001 - shutdown must never raise
            _logger.exception("api.shutdown.engine_dispose_failed")
        _logger.info("api.shutdown")


class _RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Per-request structlog context lifecycle.

    Responsibilities (every request, including failed ones):
      1. Read the inbound `X-Request-Id` header or mint a new UUIDv4 — this
         is the correlation ID for the entire request -> task chain.
      2. Bind it into structlog's contextvars so every log line under this
         request carries `request_id=...`.
      3. Echo it back on the response so the caller can quote it in a
         support ticket.
      4. CRITICALLY: clear contextvars at the end so the threadpool worker
         that handled this sync route does not leak `notification_id` /
         `request_id` into the next unrelated request that lands on it.
    """

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        # Always start from a clean slate — defends against any upstream
        # contamination from a previous request on this threadpool worker.
        clear_notification_context()
        bind_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            # Hard guarantee: the contextvars bag is empty when this request
            # leaves the middleware, regardless of success/exception.
            clear_notification_context()
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def create_app() -> FastAPI:
    """Application factory — eases testing (each test can build its own app)."""
    app = FastAPI(
        title="Notification Service",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # Order matters: this middleware must wrap every route AND every other
    # middleware so logs emitted by them carry the correlation id.
    app.add_middleware(_RequestContextMiddleware)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Liveness probe. Cheap; does NOT touch DB or broker."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    def readyz() -> dict[str, object]:
        """
        Readiness probe. Checks DB + Redis (broker / cap-counter store).

        Returns 200 with per-dependency status when all checks pass; 503
        otherwise. Suitable for Kubernetes readinessProbe / load-balancer
        target-health checks (separate from /healthz so a broker hiccup
        doesn't kill the container, only de-registers it from rotation).
        """
        from fastapi import HTTPException, status as http_status
        from sqlalchemy import text

        from app.core.config import get_settings

        components: dict[str, dict[str, str]] = {}
        overall_ok = True

        # DB: cheap SELECT 1 through the engine pool.
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            components["database"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            overall_ok = False
            components["database"] = {"status": "error", "error": str(exc)[:200]}

        # Redis (broker). We don't import celery here to keep the probe cheap.
        try:
            import redis as _redis

            client = _redis.Redis.from_url(
                get_settings().celery_broker_url, socket_timeout=2.0
            )
            client.ping()
            components["redis"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            overall_ok = False
            components["redis"] = {"status": "error", "error": str(exc)[:200]}

        body: dict[str, object] = {
            "status": "ok" if overall_ok else "error",
            "components": components,
        }
        if not overall_ok:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=body,
            )
        return body

    app.include_router(notifications_router)
    app.include_router(templates_router)
    app.include_router(preferences_router)
    app.include_router(tracking_router)
    app.include_router(management_router)
    return app


# Uvicorn entrypoint: `uvicorn app.main:app`.
app = create_app()
