"""documents and chunks

Adds PDF documents scoped to one conversation, and the chunks they are split
into, with an embedding per chunk (ADR-0015).

Hand-edited in four places. It first refuses a data directory that was
initialised under the old Alpine (musl) image and reattached to the Debian
(glibc) pgvector image, which would leave text indexes silently wrong; see
``app.db.migrations_helpers``. The ``vector`` extension is created next, and it
fails loudly on a PostgreSQL server built without pgvector, which is the point:
a server that cannot store embeddings must never reach a migrated state. The
``document_status`` enum is created and dropped explicitly, as in the earlier
migrations, so a downgrade followed by an upgrade does not fail on "type
already exists". And the downgrade drops the extension, so ``downgrade base``
still leaves an empty database.

Revision ID: 74717a8fb49f
Revises: 20f4e596ca68
Create Date: 2026-10-19 23:07:31.245642

"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.migrations_helpers import refuse_reattached_musl_volume

revision: str = "74717a8fb49f"
down_revision: str | None = "20f4e596ca68"
branch_labels: str | None = None
depends_on: str | None = None

document_status = postgresql.ENUM(
    "processing", "ready", "failed", name="document_status", create_type=False
)


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    refuse_reattached_musl_volume(op.get_bind())
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    document_status.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "documents",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("status", document_status, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("embedding_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "byte_size > 0 AND page_count > 0 AND char_count > 0 AND chunk_count > 0",
            name="ck_documents_counts_positive",
        ),
        sa.CheckConstraint("embedding_tokens >= 0", name="ck_documents_tokens_nonnegative"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "content_sha256", name="uq_documents_conversation_sha"
        ),
    )
    op.create_index(
        "ix_documents_conversation_created", "documents", ["conversation_id", "created_at"]
    )
    op.create_index(
        "ix_documents_processing_lease",
        "documents",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", VECTOR(dim=1536), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("length(content) > 0", name="ck_document_chunks_content_nonempty"),
        sa.CheckConstraint("ordinal >= 0", name="ck_document_chunks_ordinal_nonnegative"),
        sa.CheckConstraint("page > 0", name="ck_document_chunks_page_positive"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
    )
    op.create_index(
        "ix_document_chunks_content_tsv",
        "document_chunks",
        ["content_tsv"],
        postgresql_using="gin",
    )
    op.create_index("ix_document_chunks_conversation", "document_chunks", ["conversation_id"])
    op.create_index(
        "ix_document_chunks_unembedded",
        "document_chunks",
        ["document_id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Dropping a table drops its indexes with it.
    op.drop_table("document_chunks")
    op.drop_table("documents")
    document_status.drop(op.get_bind(), checkfirst=False)
    op.execute("DROP EXTENSION IF EXISTS vector")
