"""
Channel providers package.

Public surface re-exports the abstraction layer so the rest of the codebase
imports from `app.channels` only:

    from app.channels import ChannelProvider, SendOutcome, get_provider
"""

from app.channels.base import (
    ChannelProvider,
    NonRetryableProviderError,
    ProviderError,
    RetryableProviderError,
    SendOutcome,
    SendPayload,
    SendStatus,
)
from app.channels.registry import (
    get_provider,
    list_registered_channels,
    register_provider,
)

# Importing concrete providers here triggers their `@register_provider`
# decorators so the registry is populated as soon as `app.channels` loads.
# Order is irrelevant — registration is idempotent per channel.
from app.channels import email     # noqa: F401  (registers MockEmailProvider)
from app.channels import sms       # noqa: F401  (registers MockSmsProvider)
from app.channels import push      # noqa: F401  (registers MockPushProvider)
from app.channels import webhook   # noqa: F401  (registers WebhookProvider)

__all__ = [
    "ChannelProvider",
    "SendOutcome",
    "SendPayload",
    "SendStatus",
    "ProviderError",
    "RetryableProviderError",
    "NonRetryableProviderError",
    "register_provider",
    "get_provider",
    "list_registered_channels",
]
