"""
Webhook channel provider (PRD §5.5).

This is the only "real" provider — it actually performs an HTTP POST to the
URL configured on the user (or supplied as `recipient_address`). Failure
classification follows standard HTTP semantics:

  - 2xx                              → success ("acknowledged" if 200/204)
  - 408 / 425 / 429 / 5xx            → RetryableProviderError
  - other 4xx                        → NonRetryableProviderError
  - network / timeout / DNS errors    → RetryableProviderError
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

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

# HTTP status codes that should trigger a retry (transient by spec).
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)


@dataclass(frozen=True, slots=True)
class WebhookProviderConfig:
    request_timeout_seconds: float

    @classmethod
    def from_settings(cls) -> "WebhookProviderConfig":
        s = get_settings()
        return cls(request_timeout_seconds=s.webhook_request_timeout_seconds)


@register_provider
class WebhookProvider(ChannelProvider):
    """Webhook provider — POSTs the notification payload to the configured URL."""

    channel_type = ChannelType.WEBHOOK

    def __init__(self, config: WebhookProviderConfig | None = None) -> None:
        self._config = config or WebhookProviderConfig.from_settings()

    def validate_address(self, address: str) -> None:
        """
        Reject obviously malformed URLs.

        Strict scheme check: only http/https. file:// and friends are
        intentionally rejected to prevent SSRF-via-config-typo (defense in
        depth — operators should still allow-list webhook hosts upstream).
        """
        if not address or not isinstance(address, str):
            raise NonRetryableProviderError("Webhook URL must be a non-empty string.")
        if not (address.startswith("http://") or address.startswith("https://")):
            raise NonRetryableProviderError(
                f"Webhook URL must use http(s) scheme: {address!r}"
            )
        if len(address) > 2048:
            raise NonRetryableProviderError("Webhook URL exceeds 2048 chars.")

    def send(self, payload: SendPayload) -> SendOutcome:
        """Perform a single POST attempt to the webhook URL."""
        self.validate_address(payload.recipient_address)

        request_id = f"mock-wh-{uuid.uuid4()}"
        body = self._build_request_body(payload, request_id)

        _logger.info(
            "webhook.attempt",
            notification_id=payload.notification_id,
            url=payload.recipient_address,
            request_id=request_id,
        )

        try:
            with httpx.Client(timeout=self._config.request_timeout_seconds) as client:
                response = client.post(
                    payload.recipient_address,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Notification-Id": payload.notification_id,
                        "X-Request-Id": request_id,
                    },
                )
        except httpx.TimeoutException as exc:
            _logger.warning("webhook.timeout", request_id=request_id, error=str(exc))
            raise RetryableProviderError(f"Webhook timeout: {exc}") from exc
        except httpx.RequestError as exc:
            # Connect errors, DNS, TLS handshake — all transient by definition.
            _logger.warning("webhook.network_error", request_id=request_id, error=str(exc))
            raise RetryableProviderError(f"Webhook network error: {exc}") from exc

        return self._classify_response(payload, response, request_id)

    # --- Helpers ---------------------------------------------------------

    @staticmethod
    def _build_request_body(payload: SendPayload, request_id: str) -> dict:
        """The JSON body sent to the webhook URL."""
        return {
            "request_id": request_id,
            "notification_id": payload.notification_id,
            "subject": payload.subject,
            "title": payload.title,
            "body": payload.body,
            "data": payload.data,
        }

    def _classify_response(
        self,
        payload: SendPayload,
        response: httpx.Response,
        request_id: str,
    ) -> SendOutcome:
        """Map HTTP status code → SendOutcome / exception."""
        status_code = response.status_code

        if 200 <= status_code < 300:
            _logger.info(
                "webhook.acknowledged",
                request_id=request_id,
                status_code=status_code,
            )
            return SendOutcome(
                status=SendStatus.DELIVERED,
                provider_response={
                    "provider": "webhook",
                    "request_id": request_id,
                    # Lifecycle event per §5.5: sent / acknowledged / failed.
                    "webhook_event": "acknowledged",
                    "status_code": status_code,
                    "response_excerpt": response.text[:512],
                },
            )

        if status_code in _RETRYABLE_STATUS_CODES:
            _logger.warning(
                "webhook.retryable_status",
                request_id=request_id,
                status_code=status_code,
            )
            raise RetryableProviderError(
                f"Webhook returned retryable status {status_code}."
            )

        _logger.warning(
            "webhook.permanent_status",
            request_id=request_id,
            status_code=status_code,
        )
        raise NonRetryableProviderError(
            f"Webhook returned non-retryable status {status_code}."
        )
