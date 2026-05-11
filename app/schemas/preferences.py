"""Pydantic schemas for the user-preferences API (PRD §3.2)."""

from __future__ import annotations

import re
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ChannelType

# Allow http(s) only; same defense-in-depth as WebhookProvider.validate_address.
# Host part must contain a dot to reject obviously bogus URLs like "https://x".
_WEBHOOK_URL_RE = re.compile(r"^https?://[^\s/]+\.[^\s]{0,2046}$")

# Pragmatic email + E.164 patterns (mirror the channel providers).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


def _validate_timezone(tz: str | None) -> str | None:
    """Reject anything `zoneinfo` can't load."""
    if tz is None:
        return None
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {tz!r}") from exc
    return tz


def _validate_frequency_caps(caps: dict[str, Any]) -> dict[str, int | None]:
    """Whitelist allowed cap windows; coerce values to non-negative ints."""
    allowed_keys = {"per_hour", "per_day"}
    bad_keys = set(caps).difference(allowed_keys)
    if bad_keys:
        raise ValueError(
            f"Unsupported frequency_caps keys: {sorted(bad_keys)}; "
            f"allowed: {sorted(allowed_keys)}"
        )
    cleaned: dict[str, int | None] = {}
    for k, v in caps.items():
        if v is None:
            cleaned[k] = None
            continue
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"frequency_caps[{k!r}] must be a non-negative int or null.")
        cleaned[k] = v
    return cleaned


class UserPreferencesUpsertRequest(BaseModel):
    """
    Body of `PUT /users/{user_id}/preferences`.

    Full-replacement semantics (PUT, not PATCH): the request fully describes
    the user's preference state. Missing optional fields reset to defaults.
    """

    model_config = ConfigDict(extra="forbid")

    enabled_channels: list[ChannelType] = Field(default_factory=list)
    per_type_preferences: dict[str, list[ChannelType]] = Field(default_factory=dict)

    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    quiet_hours_timezone: str | None = Field(default=None, max_length=64)

    frequency_caps: dict[str, int | None] = Field(default_factory=dict)

    webhook_url: str | None = Field(default=None, max_length=2048)
    # Per-channel destination addresses. Required when the corresponding
    # channel is enabled (enforced in `_addresses_required_when_enabled`).
    email_address: str | None = Field(default=None, max_length=320)
    phone_number: str | None = Field(default=None, max_length=32)
    device_token: str | None = Field(default=None, max_length=4096)
    is_paused: bool = False

    # --- Validators ------------------------------------------------------

    @field_validator("enabled_channels")
    @classmethod
    def _no_duplicates(cls, v: list[ChannelType]) -> list[ChannelType]:
        if len(set(v)) != len(v):
            raise ValueError("enabled_channels must not contain duplicates.")
        return v

    @field_validator("per_type_preferences")
    @classmethod
    def _per_type_no_duplicates(
        cls, v: dict[str, list[ChannelType]]
    ) -> dict[str, list[ChannelType]]:
        for key, channels in v.items():
            if not key or not key.strip():
                raise ValueError("per_type_preferences keys must be non-empty.")
            if len(set(channels)) != len(channels):
                raise ValueError(
                    f"per_type_preferences[{key!r}] contains duplicate channels."
                )
        return v

    @field_validator("quiet_hours_timezone")
    @classmethod
    def _tz_must_be_known(cls, v: str | None) -> str | None:
        return _validate_timezone(v)

    @field_validator("frequency_caps")
    @classmethod
    def _caps_well_formed(cls, v: dict[str, Any]) -> dict[str, int | None]:
        return _validate_frequency_caps(v)

    @field_validator("webhook_url")
    @classmethod
    def _webhook_url_well_formed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _WEBHOOK_URL_RE.match(v):
            raise ValueError("webhook_url must be a valid http(s) URL.")
        return v

    @field_validator("email_address")
    @classmethod
    def _email_well_formed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _EMAIL_RE.match(v):
            raise ValueError(f"email_address {v!r} is not a valid email.")
        return v

    @field_validator("phone_number")
    @classmethod
    def _phone_well_formed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _PHONE_RE.match(v):
            raise ValueError(
                f"phone_number {v!r} must be E.164 format (e.g. +12345678901)."
            )
        return v

    @field_validator("device_token")
    @classmethod
    def _device_token_well_formed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) < 16:
            raise ValueError("device_token must be at least 16 chars.")
        return v

    @model_validator(mode="after")
    def _quiet_hours_consistency(self) -> "UserPreferencesUpsertRequest":
        """All-or-nothing: if any quiet-hours field is set, all three must be."""
        triple = (self.quiet_hours_start, self.quiet_hours_end, self.quiet_hours_timezone)
        any_set = any(v is not None for v in triple)
        all_set = all(v is not None for v in triple)
        if any_set and not all_set:
            raise ValueError(
                "quiet_hours_start, quiet_hours_end, and quiet_hours_timezone "
                "must be set together (or all left null)."
            )
        return self

    @model_validator(mode="after")
    def _addresses_required_when_enabled(self) -> "UserPreferencesUpsertRequest":
        """
        If a channel is in `enabled_channels`, its destination address must be
        present. Prevents shipping a profile that resolves to zero deliverable
        channels at dispatch time.
        """
        required: list[tuple[ChannelType, str, str | None]] = [
            (ChannelType.WEBHOOK, "webhook_url", self.webhook_url),
            (ChannelType.EMAIL, "email_address", self.email_address),
            (ChannelType.SMS, "phone_number", self.phone_number),
            (ChannelType.PUSH, "device_token", self.device_token),
        ]
        for channel, field_name, value in required:
            if channel in self.enabled_channels and not value:
                raise ValueError(
                    f"{field_name} is required when {channel.value!r} is in "
                    f"enabled_channels."
                )
        return self


class UserPreferencesResponse(BaseModel):
    user_id: str
    enabled_channels: list[ChannelType]
    per_type_preferences: dict[str, list[ChannelType]]
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    quiet_hours_timezone: str | None
    frequency_caps: dict[str, int | None]
    webhook_url: str | None
    email_address: str | None
    phone_number: str | None
    device_token: str | None
    is_paused: bool
