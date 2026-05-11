"""
Mock Push notification provider (PRD §5.4).

Capabilities:
  - Validates a non-empty device token (format is vendor-specific; we only
    sanity-check length to catch obvious garbage).
  - Carries `title`, `body`, and a JSON-serializable `data` payload.
  - Simulates the three statuses required by §5.4:
        sent → delivered → clicked       (happy path; "clicked" sometimes)
             → failed                    (invalid token, non-retryable)
    Plus a transient-error path → RetryableProviderError.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from app.channels.base import (
    ChannelProvider,
    NonRetryableProviderError,
    RetryableProviderError,
    SendOutcome,
    SendPayload,
    SendStatus,
)
from app.channels.registry import register_provider
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import ChannelType

_logger = get_logger(__name__)

# FCM tokens are typically 152+ chars; APNs ~64 hex. Pick a conservative floor.
_MIN_DEVICE_TOKEN_LEN = 16
_MAX_DEVICE_TOKEN_LEN = 4096


@dataclass(frozen=True, slots=True)
class PushProviderConfig:
    transient_failure_rate: float
    invalid_token_rate: float
    clicked_rate: float

    @classmethod
    def from_settings(cls) -> "PushProviderConfig":
        s = get_settings()
        return cls(
            transient_failure_rate=s.push_transient_failure_rate,
            invalid_token_rate=s.push_invalid_token_rate,
            clicked_rate=s.push_clicked_rate,
        )


@register_provider
class MockPushProvider(ChannelProvider):
    """Mocked push notification provider."""

    channel_type = ChannelType.PUSH

    def __init__(self, config: PushProviderConfig | None = None) -> None:
        self._config = config or PushProviderConfig.from_settings()

    def validate_address(self, address: str) -> None:
        """Reject obviously malformed device tokens."""
        if not address or not isinstance(address, str):
            raise NonRetryableProviderError("Device token must be a non-empty string.")
        n = len(address)
        if n < _MIN_DEVICE_TOKEN_LEN or n > _MAX_DEVICE_TOKEN_LEN:
            raise NonRetryableProviderError(
                f"Device token length {n} outside acceptable bounds "
                f"[{_MIN_DEVICE_TOKEN_LEN}, {_MAX_DEVICE_TOKEN_LEN}]."
            )

    def send(self, payload: SendPayload) -> SendOutcome:
        """
        Simulate one push delivery attempt.

        Push requires a `title`. If the caller didn't provide one we infer
        it from the payload body's first line — easier on integrators than
        bouncing the request.
        """
        self.validate_address(payload.recipient_address)
        title = payload.title or self._derive_title(payload.body)
        if not title:
            raise NonRetryableProviderError(
                "Push notification requires a non-empty `title`."
            )
        if not payload.body or not payload.body.strip():
            raise NonRetryableProviderError("Push body must be non-empty.")

        message_id = f"mock-push-{uuid.uuid4()}"
        _logger.info(
            "push.attempt",
            notification_id=payload.notification_id,
            device_token_prefix=payload.recipient_address[:8],
            has_data=bool(payload.data),
            message_id=message_id,
        )

        # Invalid-token outcomes are non-retryable — the client must update
        # the registered token on next app launch.
        if random.random() < self._config.invalid_token_rate:
            _logger.warning("push.invalid_token", message_id=message_id)
            raise NonRetryableProviderError("Mock push: invalid/expired device token.")

        if random.random() < self._config.transient_failure_rate:
            _logger.warning("push.transient_error", message_id=message_id)
            raise RetryableProviderError("Mock push transient error (FCM 5xx).")

        push_event = "clicked" if random.random() < self._config.clicked_rate else "delivered"

        _logger.info(
            "push.success",
            notification_id=payload.notification_id,
            message_id=message_id,
            push_event=push_event,
        )
        return SendOutcome(
            status=SendStatus.DELIVERED,
            provider_response={
                "provider": "mock_push",
                "message_id": message_id,
                # Lifecycle event per §5.4: sent / delivered / clicked.
                "push_event": push_event,
                "title": title,
                "data_keys": sorted(payload.data.keys()),
            },
        )

    @staticmethod
    def _derive_title(body: str) -> str:
        """Use the first line of the body as a fallback title (max 80 chars)."""
        if not body:
            return ""
        first_line = body.splitlines()[0].strip()
        return first_line[:80]
