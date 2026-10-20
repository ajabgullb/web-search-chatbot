"""embedding usage ledger

Append-only record of embedding tokens spent per user, behind the daily
embedding quota (ADR-0015). Not linked to documents, so deleting a document
does not refund its tokens.

Hand-edited: the ``embedding_purpose`` enum is created and dropped
explicitly, as in the earlier migrations.

Revision ID: 59742e5860d8
Revises: 74717a8fb49f
Create Date: 2026-10-20 12:04:31.512418

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "59742e5860d8"
down_revision: str | None = "74717a8fb49f"
branch_labels: str | None = None
depends_on: str | None = None

embedding_purpose = postgresql.ENUM(
    "document", "query", name="embedding_purpose", create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    embedding_purpose.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "embedding_usage",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("purpose", embedding_purpose, nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint("tokens > 0", name="ck_embedding_usage_tokens_positive"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_embedding_usage_user_created", "embedding_usage", ["user_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("embedding_usage")
    embedding_purpose.drop(op.get_bind(), checkfirst=False)
