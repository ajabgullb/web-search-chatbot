"""llm credentials

Users' own LLM API keys (ADR-0020): one row per user and provider, the key
encrypted with AES-256-GCM under CREDENTIAL_ENCRYPTION_KEY and bound to the
owner's id. A partial unique index allows one active row per user: the model
their answers come from.

Reversible: the downgrade drops the table, which deletes every saved key.
Users fall back to the server's model, or are asked for a key again.

Revision ID: c3f1e8a2b7d4
Revises: a7d6227782ab
Create Date: 2026-11-10 10:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c3f1e8a2b7d4"
down_revision: str | None = "a7d6227782ab"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "llm_credentials",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column("key_hint", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_llm_credentials_user_id_provider"),
    )
    op.create_index(
        "uq_llm_credentials_one_active_per_user",
        "llm_credentials",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_llm_credentials_one_active_per_user", table_name="llm_credentials")
    op.drop_table("llm_credentials")
