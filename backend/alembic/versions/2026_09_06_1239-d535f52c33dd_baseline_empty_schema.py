"""baseline empty schema

The anchor revision. It creates nothing: it exists so that every later
migration has a stable parent and so `upgrade head` / `downgrade base` are
exercised end to end from Phase 1 onward, on a genuinely empty database.

Revision ID: d535f52c33dd
Revises:
Create Date: 2026-09-06 12:39:19.849925
"""

from __future__ import annotations

revision: str = "d535f52c33dd"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op: the schema starts empty."""


def downgrade() -> None:
    """No-op: nothing to undo."""
