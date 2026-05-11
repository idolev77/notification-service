"""
Mock SMS channel provider (PRD §5.3).

Capabilities:
  - Validates an E.164-ish phone number.
  - Enforces a per-message character limit (sms_max_chars from Settings).
  - Simulates the three statuses required by §5.3:
        sent → delivered                 (happy path)
             → failed                    (carrier-rejected, non-retryable)
    Plus a transient-error path that surfaces as RetryableProviderError.

Lifecycle event tracked in `provider_response.sms_event` so the worker's
SendStatus stays attempt-scoped (same separation as the email provider).
"""

from __future__ import annotations

import random
import re
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

# Loose E.164 — `+` then 8–15 digits. Strict enough to reject obvious garbage,
# permissive enough to avoid false negatives on valid international numbers.
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


@dataclass(frozen=True, slots=True)
class SmsProviderConfig:
    """Channel-specific configuration (PRD §5.1)."""

    max_chars: int
    transient_failure_rate: float
    failed_rate: float

    @classmethod
    def from_settings(cls) -> "SmsProviderConfig":
        s = get_settings()
        return cls(
            max_chars=s.sms_max_chars,
            transient_failure_rate=s.sms_transient_failure_rate,
            failed_rate=s.sms_failed_rate,
        )


@register_provider
class MockSmsProvider(ChannelProvider):
    """Mocked SMS provider that logs and randomly succeeds/fails."""

    channel_type = ChannelType.SMS

    def __init__(self, config: SmsProviderConfig | None = None) -> None:
        self._config = config or SmsProviderConfig.from_settings()

    def validate_address(self, address: str) -> None:
        """Reject obviously malformed phone numbers."""
        if not address or not isinstance(address, str):
            raise NonRetryableProviderError("Phone number must be a non-empty string.")
        if not _PHONE_RE.match(address):
            raise NonRetryableProviderError(
                f"Malformed phone number {address!r}; expected E.164 (+12345678901)."
            )

    def send(self, payload: SendPayload) -> SendOutcome:
        """
        Simulate one SMS delivery attempt.

        Decision tree (independent rolls; non-retryable wins over transient):
          1. Validate address + body length.
          2. P=failed_rate                → NonRetryable (carrier rejected).
          3. P=transient_failure_rate      → Retryable.
          4. Otherwise                     → SUCCESS (delivered).
        """
        self.validate_address(payload.recipient_address)
        body = self._validate_and_truncate_body(payload.body)

        message_id = f"mock-sms-{uuid.uuid4()}"
        _logger.info(
            "sms.attempt",
            notification_id=payload.notification_id,
            recipient=payload.recipient_address,
            body_len=len(body),
            message_id=message_id,
        )

        if random.random() < self._config.failed_rate:
            _logger.warning(
                "sms.failed",
                notification_id=payload.notification_id,
                message_id=message_id,
            )
            raise NonRetryableProviderError(
                "Mock SMS hard failure (carrier rejected)."
            )

        if random.random() < self._config.transient_failure_rate:
            _logger.warning(
                "sms.transient_error",
                notification_id=payload.notification_id,
                message_id=message_id,
            )
            raise RetryableProviderError("Mock SMS transient error (carrier 5xx).")

        _logger.info(
            "sms.success",
            notification_id=payload.notification_id,
            message_id=message_id,
        )
        return SendOutcome(
            status=SendStatus.DELIVERED,
            provider_response={
                "provider": "mock_sms",
                "message_id": message_id,
                # Lifecycle event per §5.3: sent / delivered / failed.
                "sms_event": "delivered",
                "body_chars": len(body),
            },
        )

    # --- Helpers ---------------------------------------------------------

    def _validate_and_truncate_body(self, body: str) -> str:
        """
        Enforce SMS character limit (PRD §5.3 "Handle character limit").

        Strategy: silently TRUNCATE rather than fail. Carriers segment long
        messages, but our brief is "handle character limit" — truncation is
        the safe default; a future provider could opt to multi-segment instead.
        Documented in DECISIONS.md.
        """
        if not body or not body.strip():
            raise NonRetryableProviderError("SMS body must be non-empty.")
        if len(body) <= self._config.max_chars:
            return body
        truncated = body[: self._config.max_chars]
        _logger.warning(
            "sms.truncated",
            original_len=len(body),
            new_len=len(truncated),
            limit=self._config.max_chars,
        )
        return truncated
