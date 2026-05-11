"""
Test 3 — Retry on failure (PRD §7.5 + §4.5).

Validates two distinct contracts:

  (a) The mock email provider correctly classifies failures:
      - bounce_rate=1.0 → NonRetryableProviderError
      - transient_failure_rate=1.0 → RetryableProviderError

  (b) The Celery base task `_DeliveryTask` is configured so Celery's
      autoretry machinery covers retryable errors but NOT non-retryable
      ones (autoretry_for must contain only RetryableProviderError).
"""

from __future__ import annotations

import pytest

from app.channels.base import (
    NonRetryableProviderError,
    RetryableProviderError,
    SendPayload,
)
from app.channels.email import EmailProviderConfig, MockEmailProvider
from app.tasks.deliver import _DeliveryTask


def _payload() -> SendPayload:
    return SendPayload(
        notification_id="notif-1",
        recipient_address="ada@example.com",
        body="hello",
    )


def test_email_bounce_raises_non_retryable() -> None:
    provider = MockEmailProvider(
        config=EmailProviderConfig(
            transient_failure_rate=0.0,
            bounce_rate=1.0,
            open_rate=0.0,
        )
    )
    with pytest.raises(NonRetryableProviderError):
        provider.send(_payload())


def test_email_transient_failure_raises_retryable() -> None:
    provider = MockEmailProvider(
        config=EmailProviderConfig(
            transient_failure_rate=1.0,
            bounce_rate=0.0,
            open_rate=0.0,
        )
    )
    with pytest.raises(RetryableProviderError):
        provider.send(_payload())


def test_invalid_address_is_non_retryable_before_send() -> None:
    """validate_address must reject malformed input as PERMANENTLY_FAILED."""
    provider = MockEmailProvider(
        config=EmailProviderConfig(
            transient_failure_rate=0.0, bounce_rate=0.0, open_rate=0.0
        )
    )
    with pytest.raises(NonRetryableProviderError):
        provider.validate_address("not-an-email")


def test_celery_task_only_autoretries_retryable_errors() -> None:
    """
    Critical safety property: NonRetryableProviderError must NOT appear in
    `autoretry_for`, otherwise Celery would retry hard-failed deliveries
    and waste worker capacity until max_retries.
    """
    assert RetryableProviderError in _DeliveryTask.autoretry_for
    assert NonRetryableProviderError not in _DeliveryTask.autoretry_for
    assert _DeliveryTask.retry_backoff is True
    assert _DeliveryTask.retry_jitter is True
    assert _DeliveryTask.acks_late is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_soft_time_limit_exceeded_is_in_autoretry_for() -> None:
    """
    SoftTimeLimitExceeded must be in `autoretry_for` so that a task killed
    by Celery's soft-timeout is automatically requeued rather than silently
    dropped. Without this the task would disappear from the queue with no
    delivery attempt recorded.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    assert SoftTimeLimitExceeded in _DeliveryTask.autoretry_for


def test_max_retries_is_finite() -> None:
    """
    `max_retries` must be a finite positive integer. A value of `None` would
    mean infinite retries, causing a permanently-failing delivery to pin a
    worker queue indefinitely and exhaust the dead-letter budget.
    """
    assert _DeliveryTask.max_retries is not None
    assert isinstance(_DeliveryTask.max_retries, int)
    assert _DeliveryTask.max_retries >= 1


def test_retry_backoff_max_caps_wait_time() -> None:
    """
    `retry_backoff_max` prevents exponential backoff from producing
    arbitrarily long wait times (e.g. 2^20 seconds). A missing or zero cap
    would starve subsequent tasks behind a stuck delivery for hours.
    """
    assert hasattr(_DeliveryTask, "retry_backoff_max")
    assert isinstance(_DeliveryTask.retry_backoff_max, (int, float))
    assert _DeliveryTask.retry_backoff_max > 0


def test_sms_provider_address_validation_rejects_plaintext_number() -> None:
    """
    SMS providers must reject a phone number without the leading `+` (E.164
    requires the country-code prefix). A plaintext number like `0501234567`
    must raise NonRetryableProviderError at validate_address time — before
    any outbound network call is made.
    """
    from app.channels.sms import MockSmsProvider, SmsProviderConfig

    provider = MockSmsProvider(
        config=SmsProviderConfig(
            max_chars=160,
            transient_failure_rate=0.0,
            failed_rate=0.0,
        )
    )
    with pytest.raises(NonRetryableProviderError):
        provider.validate_address("0501234567")  # missing leading `+`
