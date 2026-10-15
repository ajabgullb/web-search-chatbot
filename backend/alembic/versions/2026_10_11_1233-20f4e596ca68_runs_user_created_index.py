"""runs: index for the daily per-user quota

Every run submission counts the caller's runs in the last 24 hours. This index
on ``(user_id, created_at)`` turns that count into a short range scan instead
of a pass over every run the user has ever made.

A plain CREATE INDEX, which blocks writes to ``runs`` while it builds. That is
a moment on a v1-sized table; a large live table would want
``CREATE INDEX CONCURRENTLY`` outside a transaction instead.

Revision ID: 20f4e596ca68
Revises: 10bb44746c2b
Create Date: 2026-10-11 12:33:01.491177
"""

from __future__ import annotations

from alembic import op

revision: str = "20f4e596ca68"
down_revision: str | None = "10bb44746c2b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_runs_user_created", "runs", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_user_created", table_name="runs")
