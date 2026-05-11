"""Add version_id (OCC) to user_preferences and templates.

Revision ID: 0003_optimistic_concurrency
Revises: 0002_per_channel_addresses
Create Date: 2026-05-11 00:00:02

Adds an integer `version_id` column to both mutable resource tables so
SQLAlchemy's `version_id_col` mapper option can enforce optimistic
concurrency control. The column is exposed to API clients as the strong
ETag value and required (via `If-Match`) on PUT/PATCH/DELETE.

Backfill: existing rows get version_id=1. NOT NULL is then enforced.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_optimistic_concurrency"
down_revision: Union[str, None] = "0002_per_channel_addresses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add as nullable first so the table-rewrite is fast on large tables,
    # backfill, then enforce NOT NULL + default.
    for table in ("user_preferences", "templates"):
        op.add_column(
            table,
            sa.Column("version_id", sa.Integer(), nullable=True),
        )
        op.execute(f"UPDATE {table} SET version_id = 1 WHERE version_id IS NULL")
        op.alter_column(
            table,
            "version_id",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )


def downgrade() -> None:
    op.drop_column("templates", "version_id")
    op.drop_column("user_preferences", "version_id")
