"""Per-channel destination addresses on user_preferences.

Revision ID: 0002_per_channel_addresses
Revises: 0001_initial_schema
Create Date: 2026-05-11 00:00:01

Adds `email_address`, `phone_number`, `device_token` so a notification sent
with only `recipient_user_id` can resolve a channel-specific address at
dispatch time (closes the gap documented in DECISIONS.md \u00a75).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_per_channel_addresses"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("email_address", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "user_preferences",
        sa.Column("phone_number", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "user_preferences",
        sa.Column("device_token", sa.String(length=4096), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "device_token")
    op.drop_column("user_preferences", "phone_number")
    op.drop_column("user_preferences", "email_address")
