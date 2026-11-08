"""personal access tokens

The credentials MCP clients present (ADR-0019). One row per token: its
SHA-256, a display prefix, its scopes and expiry, and when it was last used or
revoked. The check constraint is the database's copy of the scope list in
``app/models/personal_access_token.py``; add a scope in both places.

Reversible: the downgrade drops the table, which revokes every token.

Revision ID: a7d6227782ab
Revises: a49d12d3e4cc
Create Date: 2026-11-07 14:57:16.737587

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7d6227782ab"
down_revision: str | None = "a49d12d3e4cc"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "personal_access_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cardinality(scopes) > 0 "
            "AND scopes <@ ARRAY['conversations:read', 'runs:create']::varchar[]",
            name="ck_personal_access_tokens_scopes_known",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_personal_access_tokens_user_id_revoked_at",
        "personal_access_tokens",
        ["user_id", "revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_personal_access_tokens_user_id_revoked_at", table_name="personal_access_tokens"
    )
    op.drop_table("personal_access_tokens")
