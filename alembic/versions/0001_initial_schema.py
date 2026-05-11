"""Initial schema: user_preferences, templates, notifications, deliveries.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-11 00:00:00

WHY a hand-written initial migration (instead of `alembic revision --autogenerate`):
  - Reproducible at exam time without needing a live DB to introspect.
  - Mirrors `app/models/*.py` exactly. The autogenerate flow remains
    available for future schema changes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- user_preferences -------------------------------------------------
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(length=128), primary_key=True),
        sa.Column(
            "enabled_channels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "per_type_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("quiet_hours_start", sa.Time(timezone=False), nullable=True),
        sa.Column("quiet_hours_end", sa.Time(timezone=False), nullable=True),
        sa.Column("quiet_hours_timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "frequency_caps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "is_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- templates --------------------------------------------------------
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("notification_type", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Partial unique index: one ACTIVE template per (type, channel).
    op.create_index(
        "uq_active_template_per_type_channel",
        "templates",
        ["notification_type", "channel"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # --- notifications ----------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "recipient_user_id",
            sa.String(length=128),
            sa.ForeignKey("user_preferences.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient_contact", sa.String(length=512), nullable=True),
        sa.Column("notification_type", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "priority",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_notifications_recipient_user_id",
        "notifications",
        ["recipient_user_id"],
    )
    op.create_index(
        "ix_notifications_notification_type",
        "notifications",
        ["notification_type"],
    )
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index(
        "ix_notifications_scheduled_at", "notifications", ["scheduled_at"]
    )

    # --- deliveries -------------------------------------------------------
    op.create_table(
        "deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("recipient_address", sa.String(length=2048), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "provider_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_deliveries_notification_id", "deliveries", ["notification_id"]
    )
    op.create_index("ix_deliveries_status", "deliveries", ["status"])


def downgrade() -> None:
    # Reverse order to respect FK dependencies.
    op.drop_index("ix_deliveries_status", table_name="deliveries")
    op.drop_index("ix_deliveries_notification_id", table_name="deliveries")
    op.drop_table("deliveries")

    op.drop_index("ix_notifications_scheduled_at", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_index(
        "ix_notifications_notification_type", table_name="notifications"
    )
    op.drop_index(
        "ix_notifications_recipient_user_id", table_name="notifications"
    )
    op.drop_table("notifications")

    op.drop_index(
        "uq_active_template_per_type_channel", table_name="templates"
    )
    op.drop_table("templates")

    op.drop_table("user_preferences")
