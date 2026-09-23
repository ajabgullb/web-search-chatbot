"""runs and citations

Adds agent runs and the sources their answers cite, and records on each
message whether it is a degraded answer.

Hand-edited in three places. The ``run_status`` enum is created and dropped
explicitly, as in the conversations migration, so a downgrade followed by an
upgrade does not fail on "type already exists". The partial unique index on
active runs is the database-level guarantee that a conversation has at most one
queued or running run. And LangGraph's checkpoint tables, which autogenerate
first proposed dropping, are left alone: the checkpointer owns them (see
``alembic/env.py``).

Revision ID: 10bb44746c2b
Revises: 8882388532bb
Create Date: 2026-09-23 22:52:57.594892

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "10bb44746c2b"
down_revision: str | None = "8882388532bb"
branch_labels: str | None = None
depends_on: str | None = None

run_status = postgresql.ENUM(
    "queued", "running", "completed", "failed", "cancelled", name="run_status", create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    run_status.create(op.get_bind(), checkfirst=False)

    # A server default so existing rows backfill without a data migration.
    op.add_column(
        "messages",
        sa.Column("degraded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("messages", sa.Column("notice", sa.Text(), nullable=True))

    op.create_table(
        "runs",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("user_message_id", sa.UUID(), nullable=False),
        sa.Column("assistant_message_id", sa.UUID(), nullable=True),
        sa.Column("status", run_status, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_retryable", sa.Boolean(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=50), nullable=True),
        sa.Column("search_count", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Idempotency scope: a key is unique per user, not globally, so one
        # client's key choice can never collide with another's.
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_runs_user_idempotency_key"),
    )
    # At most one queued/running run per conversation. Concurrent submissions
    # race on this index and exactly one commits.
    op.create_index(
        "ux_runs_one_active_per_conversation",
        "runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_runs_conversation_created",
        "runs",
        ["conversation_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index("ix_runs_user_message", "runs", ["user_message_id"], unique=False)

    op.create_table(
        "citations",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("display_url", sa.String(length=100), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("ordinal > 0", name="ck_citations_ordinal_positive"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "ordinal", name="uq_citations_message_ordinal"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("citations")
    op.drop_index("ix_runs_user_message", table_name="runs")
    op.drop_index("ix_runs_conversation_created", table_name="runs")
    op.drop_index(
        "ux_runs_one_active_per_conversation",
        table_name="runs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_table("runs")
    op.drop_column("messages", "notice")
    op.drop_column("messages", "degraded")
    run_status.drop(op.get_bind(), checkfirst=False)
