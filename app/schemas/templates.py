"""Pydantic schemas for the templates API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ChannelType


class TemplateCreateRequest(BaseModel):
    """Payload for `POST /templates` (PRD §3.3)."""

    model_config = ConfigDict(extra="forbid")

    notification_type: str = Field(min_length=1, max_length=128)
    channel: ChannelType
    subject: str | None = Field(default=None, max_length=512)
    body: str = Field(min_length=1, max_length=64_000)
    is_active: bool = True


class TemplateUpdateRequest(BaseModel):
    """Payload for `PATCH /templates/{id}`."""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=512)
    body: str | None = Field(default=None, min_length=1, max_length=64_000)
    is_active: bool | None = None


class TemplateResponse(BaseModel):
    id: int
    notification_type: str
    channel: ChannelType
    subject: str | None
    body: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
