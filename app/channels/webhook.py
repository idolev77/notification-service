"""
Webhook channel provider (PRD §5.5).

This is the only "real" provider — it actually performs an HTTP POST to the
URL configured on the user (or supplied as `recipient_address`). Failure
classification follows standard HTTP semantics:

  - 2xx                              → success ("acknowledged" if 200/204)
  - 408 / 425 / 429 / 5xx            → RetryableProviderError
  - other 4xx                        → NonRetryableProviderError
  - network / timeout / DNS errors    → RetryableProviderError

SSRF posture (defense in depth — operators should still allow-list webhook
hosts at the egress proxy):
  - Scheme allow-list (http/https only).
  - URL length cap.
  - DNS resolution of every A/AAAA record at send-time; ANY non-public
    address (private, loopback, link-local, multicast, reserved, the
    unspecified address, or 169.254.169.254 specifically — AWS / GCP
    IMDS) causes the send to fail permanently.
  - The actual TCP connection is pinned to the resolved IP (with the
    original `Host` header preserved) so a DNS-rebinding attacker cannot
    serve a public IP at validation-time and a private one at connect-time.
  - Redirects are NOT followed by default; a 30x to a private IP would
    sidestep validation. `webhook_follow_redirects=True` opts in.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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

# Cloud metadata service literals — explicit allow-no-list. These are
# globally routable IPs that nonetheless leak credentials / instance role
# data when reachable from the worker. Block them regardless of the
# `webhook_block_private_addresses` switch.
_BLOCKED_METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / OpenStack IMDS v1+v2
        "fd00:ec2::254",    # AWS IMDS over IPv6
        "100.100.100.200",  # Alibaba Cloud metadata
    }
)


@dataclass(frozen=True, slots=True)
class WebhookProviderConfig:
    request_timeout_seconds: float
    block_private_addresses: bool
    follow_redirects: bool

    @classmethod
    def from_settings(cls) -> "WebhookProviderConfig":
        s = get_settings()
        return cls(
            request_timeout_seconds=s.webhook_request_timeout_seconds,
            block_private_addresses=s.webhook_block_private_addresses,
            follow_redirects=s.webhook_follow_redirects,
        )


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

        NOTE: this is a *syntactic* check only. Network-level SSRF
        protection (DNS resolution + private-IP rejection + IP pinning)
        runs at send-time in `_resolve_and_pin`.
        """
        if not address or not isinstance(address, str):
            raise NonRetryableProviderError("Webhook URL must be a non-empty string.")
        parsed = urlsplit(address)
        if parsed.scheme not in ("http", "https"):
            raise NonRetryableProviderError(
                f"Webhook URL must use http(s) scheme: {address!r}"
            )
        if not parsed.hostname:
            raise NonRetryableProviderError(
                f"Webhook URL is missing a hostname: {address!r}"
            )
        if len(address) > 2048:
            raise NonRetryableProviderError("Webhook URL exceeds 2048 chars.")

    def send(self, payload: SendPayload) -> SendOutcome:
        """Perform a single POST attempt to the webhook URL."""
        self.validate_address(payload.recipient_address)

        request_id = f"mock-wh-{uuid.uuid4()}"
        body = self._build_request_body(payload, request_id)

        # SSRF gate — resolves DNS, validates EVERY answer, then returns
        # an IP-pinned URL we can hand to httpx.
        target_url, original_host = self._resolve_and_pin(payload.recipient_address)

        _logger.info(
            "webhook.attempt",
            notification_id=payload.notification_id,
            url=payload.recipient_address,
            pinned_url=target_url,
            request_id=request_id,
        )

        headers = {
            "Content-Type": "application/json",
            "X-Notification-Id": payload.notification_id,
            "X-Request-Id": request_id,
            # Preserve the user-facing Host so virtual-hosted endpoints + TLS
            # SNI keep working after we swap the hostname for the resolved IP.
            "Host": original_host,
        }

        try:
            with httpx.Client(
                timeout=self._config.request_timeout_seconds,
                follow_redirects=self._config.follow_redirects,
            ) as client:
                response = client.post(target_url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            _logger.warning("webhook.timeout", request_id=request_id, error=str(exc))
            raise RetryableProviderError(f"Webhook timeout: {exc}") from exc
        except httpx.RequestError as exc:
            # Connect errors, DNS, TLS handshake — all transient by definition.
            _logger.warning("webhook.network_error", request_id=request_id, error=str(exc))
            raise RetryableProviderError(f"Webhook network error: {exc}") from exc

        return self._classify_response(payload, response, request_id)

    # --- Helpers ---------------------------------------------------------

    def _resolve_and_pin(self, url: str) -> tuple[str, str]:
        """
        Resolve `url`'s host, reject any private/metadata target, and return
        a URL where the host has been replaced by the resolved IP literal.

        Returns: (pinned_url, original_host_header)
        Raises:  NonRetryableProviderError on any policy violation.
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""

        # Hostname might already be an IP literal (incl. bracketed IPv6).
        # Validate it directly without DNS.
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            self._enforce_address_policy(literal, host)
            # IP literal is already pinned; just preserve as-is.
            return url, host

        # Hostname → resolve every A/AAAA record. ALL must be public.
        try:
            addr_infos = socket.getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            # DNS failures are transient — let Celery retry.
            raise RetryableProviderError(
                f"Webhook DNS resolution failed for {host!r}: {exc}"
            ) from exc

        resolved: list[ipaddress._BaseAddress] = []
        for info in addr_infos:
            sockaddr = info[4]
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            self._enforce_address_policy(ip, host)
            resolved.append(ip)

        if not resolved:
            raise NonRetryableProviderError(
                f"Webhook host {host!r} resolved to no usable addresses."
            )

        # Pin the connection to the first resolved IP. Bracket IPv6.
        first = resolved[0]
        netloc_host = f"[{first}]" if first.version == 6 else str(first)
        # Preserve port if explicitly given.
        if parsed.port is not None:
            netloc = f"{netloc_host}:{parsed.port}"
        else:
            netloc = netloc_host
        # Keep userinfo intact if present (rare for webhooks, but possible).
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo = f"{userinfo}:{parsed.password}"
            netloc = f"{userinfo}@{netloc}"

        pinned = urlunsplit(
            (parsed.scheme, netloc, parsed.path or "/", parsed.query, parsed.fragment)
        )
        return pinned, host

    def _enforce_address_policy(
        self, ip: "ipaddress._BaseAddress", host_for_log: str
    ) -> None:
        """Raise NonRetryableProviderError if `ip` is on the deny-list."""
        # Always block known cloud-metadata literals, even if the operator
        # disabled private-address blocking.
        if str(ip) in _BLOCKED_METADATA_IPS:
            _logger.warning(
                "webhook.ssrf_blocked_metadata", host=host_for_log, ip=str(ip)
            )
            raise NonRetryableProviderError(
                f"Webhook target {host_for_log!r} resolves to a blocked "
                f"metadata service address ({ip})."
            )

        if not self._config.block_private_addresses:
            return

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            _logger.warning(
                "webhook.ssrf_blocked_private", host=host_for_log, ip=str(ip)
            )
            raise NonRetryableProviderError(
                f"Webhook target {host_for_log!r} resolves to a "
                f"non-public address ({ip}); refusing to connect."
            )

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
