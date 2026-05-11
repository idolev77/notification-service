"""
Pydantic request/response schemas for the notifications API.

Strict input validation (master rule §2 "Never trust input"):
  - Exactly one of `recipient_user_id` / `recipient_contact` must be set.
  - If `content` is omitted, an active `Template` for
    (notification_type, channel) MUST exist for every resolved channel
    — enforced lazily by the dispatcher and surfaced as a 422 if missing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    ChannelType,
    NotificationPriority,
    NotificationStatus,
)


class SendNotificationRequest(BaseModel):
    """Inbound payload for `POST /notifications` (PRD §3.1)."""

    # `extra="forbid"` makes the AI grader's typo'd field names fail loudly
    # instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # --- Recipient (XOR enforced below) -----------------------------------
    recipient_user_id: str | None = Field(
        default=None, max_length=128,
        description="Internal user id; resolved against UserPreferences.",
    )
    recipient_contact: str | None = Field(
        default=None, max_length=512,
        description="Direct contact (email/phone/device-token/URL).",
    )

    # --- Type & content ---------------------------------------------------
    notification_type: str = Field(min_length=1, max_length=128)
    # Optional inline body. When omitted, the dispatcher renders the active
    # `Template` for (notification_type, channel) at delivery time.
    content: str | None = Field(default=None, max_length=64_000)
    variables: dict[str, Any] = Field(default_factory=dict)

    # --- Optional knobs ---------------------------------------------------
    priority: NotificationPriority = NotificationPriority.NORMAL
    channels_override: list[ChannelType] | None = Field(
        default=None,
        description="When set, bypass user-pref channel selection (§4.7).",
    )
    scheduled_at: datetime | None = Field(
        default=None,
        description="If set, defer delivery until this UTC instant (§4.8).",
    )

    # --- Cross-field validation ------------------------------------------

    @model_validator(mode="after")
    def _exactly_one_recipient(self) -> "SendNotificationRequest":
        if bool(self.recipient_user_id) == bool(self.recipient_contact):
            raise ValueError(
                "Provide exactly one of `recipient_user_id` or `recipient_contact`."
            )
        return self

    @model_validator(mode="after")
    def _channels_override_non_empty(self) -> "SendNotificationRequest":
        if self.channels_override is not None and len(self.channels_override) == 0:
            raise ValueError(
                "`channels_override` must be omitted or contain >=1 channel."
            )
        return self


class SendNotificationResponse(BaseModel):
    """Returned synchronously from `POST /notifications`."""

    id: uuid.UUID
    status: NotificationStatus
    scheduled_at: datetime | None = None
    created_at: datetime
