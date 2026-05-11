"""Drop the unused notifications.template_id column.

Revision ID: 0004_drop_unused_template_id
Revises: 0003_optimistic_concurrency
Create Date: 2026-05-11 00:00:03

The column was carried in the initial schema as a forward-looking
"request-level template reference" but was never written: the dispatcher
resolves the active Template by (notification_type, channel) at delivery
time (see DECISIONS.md §1). Removing the dead column to keep the schema
honest and avoid an interview question with no good answer.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_drop_unused_template_id"
down_revision: Union[str, None] = "0003_optimistic_concurrency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the FK first (alembic infers the constraint name; if your
    # naming convention differs, set it explicitly).
    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("template_id")


def downgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
