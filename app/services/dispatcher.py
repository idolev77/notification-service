"""
Delivery dispatcher — the unit of work executed inside a Celery task.

Transactional layout (this is the production-grade bit, defensible at oral):

  TX1: load Delivery, transition QUEUED/FAILED → SENDING, attempts++,
       last_attempt_at = now, COMMIT.   (so the SENDING state is durable
       even if the worker crashes mid-attempt)

  Provider call happens OUTSIDE any DB transaction — long network IO must
  never hold a Postgres connection.

  TX2 (success):  status=DELIVERED, delivered_at=now, provider_response=...
  TX2 (failure):  status=FAILED, error_message=..., COMMIT, then re-raise
                  so Celery handles retry / on_failure → PERMANENTLY_FAILED.

  After every TX2 we recompute the parent Notification's aggregate status.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.channels import (
    NonRetryableProviderError,
    RetryableProviderError,
    SendOutcome,
    SendStatus,
    get_provider,
)
from app.channels.base import ChannelProvider, SendPayload
from app.core.db import session_scope
from app.core.logging import bind_notification_context, get_logger
from app.models import Delivery, Notification, Template
from app.models.enums import ChannelType, DeliveryStatus, NotificationStatus
from app.services.templates import (
    RenderedTemplate,
    TemplateRenderError,
    render_html,
    render_template,
)
from sqlalchemy import select

_logger = get_logger(__name__)


class DeliveryAlreadyClaimedError(Exception):
    """
    Raised when a competing worker holds the row-level lock on this Delivery
    (i.e. `SELECT ... FOR UPDATE SKIP LOCKED` returned no row even though
    the delivery exists). The current worker should ack the message and
    let the holder finish — re-attempting now would simply re-deliver to
    the user.
    """


class DeliveryAlreadyTerminalError(Exception):
    """
    Raised when the row is locked successfully but its status is already
    terminal (DELIVERED / PERMANENTLY_FAILED / CANCELLED). The Celery task
    should ack and exit silently — work is done.
    """


def execute_delivery_attempt(
    *, delivery_id: uuid.UUID, channel: ChannelType
) -> None:
    """
    Execute one attempt for the given Delivery. See module docstring for the
    transactional layout.

    Time complexity: O(1) DB ops + O(provider). Space: O(1).
    """
    # --- TX1: claim the delivery (QUEUED/FAILED → SENDING) -----------------
    try:
        claim = _claim_delivery_for_attempt(delivery_id, channel)
    except DeliveryAlreadyClaimedError:
        # Another worker is mid-attempt on this exact row. Drop this
        # message — the holder will write the terminal outcome.
        _logger.info(
            "delivery.skipped_concurrent_claim",
            delivery_id=str(delivery_id),
        )
        return
    except DeliveryAlreadyTerminalError as exc:
        _logger.info(
            "delivery.skipped_already_terminal",
            delivery_id=str(delivery_id),
            current_status=str(exc),
        )
        return

    if claim is None:
        # Row was abandoned by a crashed worker AND retry budget is gone.
        # Already marked PERMANENTLY_FAILED inside the claim TX — nothing
        # more to do here.
        return

    payload, provider_cls = claim
    provider = provider_cls()

    # --- Provider call OUTSIDE any open transaction ------------------------
    try:
        outcome: SendOutcome = provider.send(payload)
    except NonRetryableProviderError as exc:
        _persist_attempt_failure(delivery_id, error=str(exc))
        _logger.warning("delivery.non_retryable", error=str(exc))
        raise  # Celery's on_failure hook will mark PERMANENTLY_FAILED.
    except RetryableProviderError as exc:
        _persist_attempt_failure(delivery_id, error=str(exc))
        _logger.info("delivery.retryable", error=str(exc))
        raise  # Celery autoretry will requeue with backoff.

    # --- TX2: success ------------------------------------------------------
    _persist_attempt_success(delivery_id, outcome)


def mark_delivery_permanently_failed(
    *, delivery_id: uuid.UUID, error_message: str
) -> None:
    """
    Terminal-fail a Delivery after retries are exhausted (or on a
    NonRetryableProviderError). Called from the Celery `on_failure` hook.
    """
    with session_scope() as session:
        delivery = _load_delivery(session, delivery_id)
        delivery.status = DeliveryStatus.PERMANENTLY_FAILED
        delivery.error_message = (error_message or "")[:4096]
        session.flush()
        _recompute_notification_status(session, delivery)
        _logger.warning(
            "delivery.permanently_failed",
            delivery_id=str(delivery_id),
            error=error_message,
        )


# ---------------------------------------------------------------------------
# Transaction-bounded helpers — each owns ONE session_scope.
# ---------------------------------------------------------------------------

def _claim_delivery_for_attempt(
    delivery_id: uuid.UUID, channel: ChannelType
) -> tuple[SendPayload, type[ChannelProvider]] | None:
    """
    TX1: flip Delivery to SENDING, increment attempts, build payload.

    Concurrency guard:
      - Acquires a row-level lock with `SELECT ... FOR UPDATE SKIP LOCKED`.
        If another worker holds the row (broker redelivered the same task
        to two consumers) the SELECT returns no row and we raise
        `DeliveryAlreadyClaimedError` so the caller can ack the duplicate.
      - If the row is already in a terminal state, raise
        `DeliveryAlreadyTerminalError`. Same outcome — the duplicate Celery
        message is acked and discarded.

    Crash recovery:
      - If the row is in `SENDING` (a previous worker committed TX1 then
        died before TX2), treat this delivery as a fresh attempt:
        increment `attempts` and update `last_attempt_at`. The previous
        attempt produced no provider record so it cannot be double-counted.
      - If `attempts` already equals `max_retries`, do NOT call the provider
        again — mark the delivery PERMANENTLY_FAILED (as if Celery's retry
        budget was exhausted) and return `None` so the caller exits.

    Returns the SendPayload + the registered provider class so the caller
    can perform the (long-running) provider call without holding a tx.
    """
    from app.core.config import get_settings as _get_settings  # local: avoid cycle

    with session_scope() as session:
        # SKIP LOCKED — if a concurrent worker already holds this row, the
        # SELECT returns nothing rather than blocking. Distinguishing
        # "row missing" from "row locked" requires a second probe without
        # the lock hint.
        locked = session.execute(
            select(Delivery)
            .where(Delivery.id == delivery_id)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if locked is None:
            # Either the row does not exist OR another worker holds it.
            exists = session.execute(
                select(Delivery.id).where(Delivery.id == delivery_id)
            ).scalar_one_or_none()
            if exists is None:
                raise LookupError(f"Delivery {delivery_id} not found.")
            raise DeliveryAlreadyClaimedError(str(delivery_id))

        delivery = locked
        notification = delivery.notification  # eager-load before mutating

        # Terminal short-circuit — duplicate Celery message after the
        # holder already wrote the final outcome.
        if delivery.status in (
            DeliveryStatus.DELIVERED,
            DeliveryStatus.PERMANENTLY_FAILED,
            DeliveryStatus.CANCELLED,
        ):
            raise DeliveryAlreadyTerminalError(delivery.status.value)

        # Crash-recovery path: previous worker committed SENDING then died.
        if delivery.status is DeliveryStatus.SENDING:
            max_retries = _get_settings().max_retry_attempts
            if delivery.attempts >= max_retries:
                # Retry budget gone — terminal-fail in this very TX so the
                # parent Notification's aggregate status is recomputed
                # synchronously and the message is acked.
                delivery.status = DeliveryStatus.PERMANENTLY_FAILED
                delivery.error_message = (
                    "Recovered from stale SENDING after worker crash; "
                    "retry budget exhausted."
                )[:4096]
                session.flush()
                _recompute_notification_status(session, delivery)
                _logger.warning(
                    "delivery.recovered_stale_sending_terminal",
                    delivery_id=str(delivery_id),
                    attempts=delivery.attempts,
                )
                return None
            _logger.info(
                "delivery.recovered_from_stale_sending",
                delivery_id=str(delivery_id),
                attempts=delivery.attempts,
            )

        bind_notification_context(
            notification_id=str(notification.id),
            attempt=delivery.attempts + 1,
            channel=channel.value,
        )

        delivery.status = DeliveryStatus.SENDING
        delivery.attempts += 1
        delivery.last_attempt_at = _utcnow()

        payload = _build_payload(session, notification, delivery)
        provider_cls = get_provider(channel)

        return payload, provider_cls


def _persist_attempt_success(delivery_id: uuid.UUID, outcome: SendOutcome) -> None:
    """
    TX2-success: write the SendOutcome onto the Delivery row.

    Three cases:
      - SENT / DELIVERED         → Delivery.status = DELIVERED (terminal-ok).
      - FAILED (soft — provider returned a definitive failure without raising)
                                 → PERMANENTLY_FAILED (terminal-fail, no retry).
        We treat soft-FAILED as terminal because the provider explicitly chose
        not to raise; if it wanted a retry it would raise RetryableProviderError.
        Marking PERMANENTLY_FAILED keeps the parent Notification's status
        machine monotonic — it can never get stuck in PROCESSING here.
    """
    with session_scope() as session:
        delivery = _load_delivery(session, delivery_id)
        if outcome.status in (SendStatus.SENT, SendStatus.DELIVERED):
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = _utcnow()
        else:
            delivery.status = DeliveryStatus.PERMANENTLY_FAILED
        delivery.provider_response = outcome.provider_response
        delivery.error_message = outcome.error_message
        session.flush()
        _recompute_notification_status(session, delivery)
        _logger.info("delivery.delivered", status=delivery.status.value)


def _persist_attempt_failure(delivery_id: uuid.UUID, *, error: str) -> None:
    """
    TX2-failure: record the failed attempt's diagnostic context.

    Status is set to FAILED here. The terminal transition to
    PERMANENTLY_FAILED happens later in `mark_delivery_permanently_failed`,
    invoked from Celery's `on_failure` hook once retries are exhausted.
    """
    with session_scope() as session:
        delivery = _load_delivery(session, delivery_id)
        delivery.status = DeliveryStatus.FAILED
        delivery.error_message = (error or "")[:4096]
        session.flush()
        _recompute_notification_status(session, delivery)


# ---------------------------------------------------------------------------
# Pure helpers (no session_scope of their own).
# ---------------------------------------------------------------------------

def _load_delivery(session: Session, delivery_id: uuid.UUID) -> Delivery:
    """Fetch a Delivery (and force-load its parent Notification) for mutation."""
    delivery = session.get(Delivery, delivery_id)
    if delivery is None:
        raise LookupError(f"Delivery {delivery_id} not found.")
    _ = delivery.notification  # eager-load before returning
    return delivery


def _build_payload(
    session: Session, notification: Notification, delivery: Delivery
) -> SendPayload:
    """
    Build the channel-agnostic SendPayload for a (Notification, Delivery).

    Content resolution order:
      1. If `notification.content` is set (inline), use it verbatim — but
         still render it through Jinja so `{{vars}}` work even without a
         template row. (Documented decision: callers may pass placeholders
         inline + variables.)
      2. Else look up the active Template for
         (notification.notification_type, delivery.channel) and render its
         subject + body with `notification.variables`.
      3. Else raise NonRetryableProviderError-equivalent ValueError — the
         claim TX has already committed, so the dispatcher's outer except
         block will translate this into a permanent failure.

    Variables can additionally carry top-level rendering hints
    (`subject`, `html_body`, `title`, `data`) that override anything
    resolved from the template.
    """
    variables = notification.variables or {}
    rendered = _render_content(session, notification, delivery, variables)

    # Top-level overrides from variables: useful for ad-hoc deliveries
    # where the caller wants to bypass template-driven subject/title.
    subject_override = variables.get("subject")
    title_override = variables.get("title")
    html_override = variables.get("html_body")

    return SendPayload(
        notification_id=str(notification.id),
        recipient_address=delivery.recipient_address,
        body=rendered.body,
        subject=subject_override or rendered.subject,
        html_body=_render_html_if_present(html_override, variables),
        title=title_override or rendered.subject,  # subject doubles as title
        data=variables.get("data") or {},
    )


def _render_content(
    session: Session,
    notification: Notification,
    delivery: Delivery,
    variables: dict,
) -> RenderedTemplate:
    """Resolve + render the body (and subject, if any) for this delivery."""
    # Branch 1: inline content. Render through Jinja so `{{var}}` still works.
    if notification.content is not None:
        try:
            body = (
                render_template(
                    template=_InlineTemplate(notification.content),
                    variables=variables,
                ).body
            )
        except TemplateRenderError as exc:
            raise ValueError(f"Failed to render inline content: {exc}") from exc
        return RenderedTemplate(subject=None, body=body, html_body=None)

    # Branch 2: look up the active template for (type, channel).
    template = _load_active_template(
        session=session,
        notification_type=notification.notification_type,
        channel=delivery.channel,
    )
    if template is None:
        raise ValueError(
            f"No content and no active template for "
            f"(notification_type={notification.notification_type!r}, "
            f"channel={delivery.channel.value!r})."
        )
    try:
        return render_template(template=template, variables=variables)
    except TemplateRenderError as exc:
        raise ValueError(f"Failed to render template id={template.id}: {exc}") from exc


def _load_active_template(
    *,
    session: Session,
    notification_type: str,
    channel: ChannelType,
) -> Template | None:
    """Single active template per (type, channel) — guaranteed by partial uniq idx."""
    stmt = (
        select(Template)
        .where(
            Template.notification_type == notification_type,
            Template.channel == channel,
            Template.is_active.is_(True),
        )
        .limit(1)
    )
    return session.scalars(stmt).first()


def _render_html_if_present(
    html_source: str | None, variables: dict
) -> str | None:
    """Render an HTML override through the autoescape-on Jinja env."""
    if not html_source:
        return None
    try:
        return render_html(html_source, variables)
    except TemplateRenderError as exc:
        raise ValueError(f"Failed to render html_body: {exc}") from exc


class _InlineTemplate:
    """
    Adapter exposing the minimum surface `render_template` needs (a `.body`
    and a `.subject` attribute) for inline content. Avoids creating a
    throw-away Template row in the DB.
    """

    __slots__ = ("body", "subject")

    def __init__(self, body: str) -> None:
        self.body = body
        self.subject = None


def _recompute_notification_status(
    session: Session, delivery: Delivery
) -> None:
    """
    Aggregate per-channel Delivery statuses into the top-level Notification.

    Concurrency:
      Multiple workers may finish sibling Deliveries on the same
      Notification at roughly the same time. Each calls this function in
      its own TX2. Without serialization, two writers can compute the
      aggregate against stale snapshots and clobber each other's status
      update (a write-write race). We defend with `SELECT ... FOR UPDATE`
      on the parent Notification row — this serializes only the aggregate
      step, not the (long) provider call. Sibling Delivery rows are read
      back fresh inside the same TX so the aggregate is computed against
      committed truth.

    Rules (PRD §4.1 + §4.4 failure isolation):
      - Any non-terminal Delivery → PROCESSING.
      - All terminal AND at least one DELIVERED → COMPLETED.
      - All terminal AND none DELIVERED → FAILED.
    """
    # Take a row-level lock on the parent Notification. Any concurrent
    # worker doing the same recompute will block here until we COMMIT.
    notification = session.execute(
        select(Notification)
        .where(Notification.id == delivery.notification_id)
        .with_for_update()
    ).scalar_one()

    # Re-read sibling deliveries inside the locked TX so the aggregate is
    # consistent with the latest committed state, not a stale identity-map
    # snapshot from before the lock.
    sibling_statuses = list(
        session.execute(
            select(Delivery.status).where(
                Delivery.notification_id == notification.id
            )
        ).scalars()
    )

    terminal = {
        DeliveryStatus.DELIVERED,
        DeliveryStatus.PERMANENTLY_FAILED,
        DeliveryStatus.CANCELLED,
    }

    if not sibling_statuses or any(s not in terminal for s in sibling_statuses):
        notification.status = NotificationStatus.PROCESSING
        return

    if any(s is DeliveryStatus.DELIVERED for s in sibling_statuses):
        notification.status = NotificationStatus.COMPLETED
    else:
        notification.status = NotificationStatus.FAILED


def _utcnow() -> datetime:
    """Timezone-aware UTC datetime — matches model defaults."""
    return datetime.now(timezone.utc)
