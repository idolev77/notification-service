"""
Notification creation service.

Responsibilities (called from the API layer):
  1. Persist a `Notification` row (status=RECEIVED).
  2. Resolve the channels for this request via the preference pipeline
     (PRD §4.2). If `channels_override` is provided it bypasses the
     pipeline (PRD §4.7).
  3. Create one `Delivery` row per resolved channel (status=QUEUED),
     resolving channel-specific recipient addresses.
  4. Flip Notification → PROCESSING and enqueue per-channel tasks.

Why the split (api ↔ service ↔ task):
  - The API layer stays thin (validate + call service + return).
  - The service owns DB writes inside one transaction (the API owns the
    commit boundary).
  - The Celery task is the unit of work and only sees Delivery IDs — keeps
    replays trivial and worker logic ignorant of HTTP shapes.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import bind_notification_context, get_logger
from app.models import Delivery, Notification, UserPreferences
from app.models.enums import (
    ChannelType,
    DeliveryStatus,
    NotificationPriority,
    NotificationStatus,
)
from app.schemas.notifications import SendNotificationRequest
from app.services.preference_resolver import (
    ResolutionResult,
    resolve_channels_for_notification,
)
from app.tasks.deliver import enqueue_delivery

_logger = get_logger(__name__)


class InfrastructureUnavailableError(RuntimeError):
    """
    Raised when the dispatch pipeline cannot make a defensible decision
    because a critical dependency (currently: the Redis frequency-cap
    evaluator under fail-closed configuration) is unreachable.

    The API layer translates this into HTTP 503 with a `Retry-After` header
    rather than silently persisting a `COMPLETED` notification — see
    DECISIONS.md §4 / Design Gap fix.
    """

    def __init__(self, *, reason: str, detail: str | None = None) -> None:
        super().__init__(f"infrastructure_unavailable:{reason}")
        self.reason = reason
        self.detail = detail


def create_and_dispatch_notification(
    *, session: Session, request: SendNotificationRequest
) -> Notification:
    """
    Create the Notification + Deliveries and enqueue per-channel tasks.

    Caller (API handler) is responsible for committing the session AFTER
    this returns — keeps the unit-of-work boundary visible in the route.

    The returned `Notification` may carry transient drop-signal attributes
    (`_drop_reason`, `_drop_detail`) populated when no Deliveries were
    created. The API layer surfaces these in the response body.

    Time complexity: O(channels). Space: O(channels).
    """
    user_pref = _load_user_preferences(session, request.recipient_user_id)

    # Resolve channels FIRST so we can fail-fast on infrastructure errors
    # WITHOUT having persisted a Notification row that would lie in the
    # audit trail. A throw-away Notification object is needed to feed the
    # resolver (it reads `notification_type` and `priority`); we only
    # call `session.add` once we know the resolution succeeded.
    transient = Notification(
        recipient_user_id=request.recipient_user_id,
        recipient_contact=request.recipient_contact,
        notification_type=request.notification_type,
        content=request.content,
        variables=request.variables or {},
        priority=request.priority or NotificationPriority.NORMAL,
        status=NotificationStatus.RECEIVED,
        scheduled_at=request.scheduled_at,
    )
    resolution = resolve_channels_for_notification(
        notification=transient,
        user_pref=user_pref,
        channels_override=request.channels_override,
    )

    # Infrastructure failure (e.g. Redis cap-evaluator down + fail-closed)
    # surfaces via the resolver as a frequency-cap drop tagged with
    # `redis_unavailable`. Do NOT persist the notification; raise so the
    # API returns 503 + Retry-After.
    if (
        resolution.filtered_by_frequency_cap
        and resolution.tripped_cap_window == "redis_unavailable"
    ):
        _logger.error(
            "notification.infrastructure_unavailable",
            reason="redis_unavailable",
            recipient_user_id=request.recipient_user_id,
        )
        raise InfrastructureUnavailableError(
            reason="frequency_cap_evaluator_unavailable",
            detail="Frequency-cap evaluator (Redis) is unreachable.",
        )

    notification = _persist_notification(session, request)
    bind_notification_context(notification_id=str(notification.id))

    # Empty resolution is a legitimate outcome (paused user, quiet hours,
    # missing preferences). We persist a deliveries-free Notification and
    # mark it COMPLETED with a structured log. NOT a 4xx — the API call
    # itself was valid; the user simply isn't receiving anything.
    if not resolution.channels:
        _finalize_filtered_notification(notification, resolution)
        return notification

    deliveries = _persist_deliveries(
        session=session,
        notification=notification,
        channels=resolution.channels,
        request=request,
        user_pref=user_pref,
    )

    # Every chosen channel could still fall through if it had no resolvable
    # address (e.g. user has WEBHOOK in enabled_channels but no webhook_url).
    if not deliveries:
        _finalize_filtered_notification(
            notification, resolution, address_drop=True
        )
        return notification

    # Flush so Delivery PKs are assigned before we hand them to Celery.
    session.flush()

    # PRD §4.8: scheduled notifications are NOT enqueued now; they stay
    # in RECEIVED state until `app.tasks.scheduler.dispatch_due_notifications`
    # picks them up at/after `scheduled_at`. This keeps the lifecycle
    # observable: callers polling the API see a clear PENDING/RECEIVED
    # state until the work actually starts.
    if request.scheduled_at is not None:
        _logger.info(
            "notification.scheduled",
            scheduled_at=request.scheduled_at.isoformat(),
        )
        return notification

    # Immediate dispatch path: flip to PROCESSING and fan out the tasks.
    notification.status = NotificationStatus.PROCESSING
    for delivery in deliveries:
        enqueue_delivery(
            delivery_id=delivery.id,
            channel=delivery.channel,
            priority=notification.priority,
        )
        _logger.info(
            "notification.enqueued",
            delivery_id=str(delivery.id),
            channel=delivery.channel.value,
        )

    return notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_user_preferences(
    session: Session, user_id: str | None
) -> UserPreferences | None:
    """Return the UserPreferences row for `user_id`, or None when absent."""
    if not user_id:
        return None
    return session.get(UserPreferences, user_id)


def _persist_notification(
    session: Session, request: SendNotificationRequest
) -> Notification:
    """Insert the top-level Notification row in RECEIVED state."""
    notification = Notification(
        recipient_user_id=request.recipient_user_id,
        recipient_contact=request.recipient_contact,
        notification_type=request.notification_type,
        content=request.content,
        # Per-channel template binding happens lazily in the dispatcher's
        # `_build_payload` — it has the (notification_type, channel) context
        # required to pick the right active template row.
        variables=request.variables or {},
        priority=request.priority or NotificationPriority.NORMAL,
        status=NotificationStatus.RECEIVED,
        scheduled_at=request.scheduled_at,
    )
    session.add(notification)
    session.flush()  # assign UUID
    return notification


def _persist_deliveries(
    *,
    session: Session,
    notification: Notification,
    channels: list[ChannelType],
    request: SendNotificationRequest,
    user_pref: UserPreferences | None,
) -> list[Delivery]:
    """Create one Delivery per channel in QUEUED state."""
    deliveries: list[Delivery] = []
    for channel in channels:
        address = _resolve_recipient_address(channel, request, user_pref)
        if address is None:
            _logger.warning(
                "delivery.dropped_no_address",
                notification_id=str(notification.id),
                channel=channel.value,
            )
            continue
        delivery = Delivery(
            notification_id=notification.id,
            channel=channel,
            recipient_address=address,
            status=DeliveryStatus.QUEUED,
            attempts=0,
        )
        session.add(delivery)
        deliveries.append(delivery)
    return deliveries


def _resolve_recipient_address(
    channel: ChannelType,
    request: SendNotificationRequest,
    user_pref: UserPreferences | None,
) -> str | None:
    """
    Resolve the channel-specific delivery address.

    Priority order, per channel:
      1. The per-channel column on `UserPreferences` (e.g. `email_address`,
         `phone_number`, `device_token`, `webhook_url`).
      2. The request's free-form `recipient_contact` field, ONLY when the
         caller did not pass `recipient_user_id` (i.e. an anonymous send
         where the caller knows what kind of address they're sending to).

    A `recipient_user_id`-only request that lacks a per-channel address on
    the user profile resolves to `None` and the channel is dropped (the
    upsert validator already prevents enabling a channel without its
    address — this branch only fires for `channels_override` cases).
    """
    per_channel: dict[ChannelType, str | None] = {
        ChannelType.EMAIL: getattr(user_pref, "email_address", None) if user_pref else None,
        ChannelType.SMS: getattr(user_pref, "phone_number", None) if user_pref else None,
        ChannelType.PUSH: getattr(user_pref, "device_token", None) if user_pref else None,
        ChannelType.WEBHOOK: getattr(user_pref, "webhook_url", None) if user_pref else None,
    }
    address = per_channel.get(channel)
    if address:
        return address
    # Fall back to the explicit contact ONLY for anonymous sends; for a
    # known user we should never silently override their stored address.
    if request.recipient_user_id is None:
        return request.recipient_contact
    return None


def _finalize_filtered_notification(
    notification: Notification,
    resolution: ResolutionResult,
    *,
    address_drop: bool = False,
) -> None:
    """
    Mark a notification that produced zero deliveries as terminally COMPLETED
    and emit a structured log explaining which filter rejected it. This is
    intentionally NOT FAILED: nothing went wrong; the user simply opted out.

    Also tags the notification with transient `_drop_reason` / `_drop_detail`
    attributes so the API layer can surface a non-silent 202 — see
    DECISIONS.md §4 / Design Gap fix.
    """
    notification.status = NotificationStatus.COMPLETED
    reason, detail = _classify_drop(resolution, address_drop=address_drop)
    # SQLAlchemy ORM instances accept arbitrary attribute assignment; using
    # underscore-prefixed names keeps these out of the mapped column space.
    notification._drop_reason = reason  # type: ignore[attr-defined]
    notification._drop_detail = detail  # type: ignore[attr-defined]
    _logger.info(
        "notification.filtered",
        notification_id=str(notification.id),
        used_override=resolution.used_override,
        filtered_by_pause=resolution.filtered_by_pause,
        filtered_by_quiet_hours=resolution.filtered_by_quiet_hours,
        filtered_by_frequency_cap=resolution.filtered_by_frequency_cap,
        tripped_cap_window=resolution.tripped_cap_window,
        missing_preferences=resolution.missing_preferences,
        drop_reason=reason,
    )


def _classify_drop(
    resolution: ResolutionResult, *, address_drop: bool
) -> tuple[str | None, str | None]:
    """Map a resolver outcome to the API-visible (drop_reason, drop_detail) pair."""
    if resolution.filtered_by_pause:
        return "paused", None
    if resolution.filtered_by_quiet_hours:
        return "quiet_hours", None
    if resolution.filtered_by_frequency_cap:
        return "frequency_cap", resolution.tripped_cap_window
    if resolution.missing_preferences:
        return "missing_preferences", None
    if address_drop:
        return "no_resolvable_address", None
    # Empty `enabled_channels` (or per-type narrowed to empty) — user has
    # not opted in to any channel for this notification type.
    return "no_enabled_channels", None
