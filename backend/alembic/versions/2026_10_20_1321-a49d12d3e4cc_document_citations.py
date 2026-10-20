"""document citations

Citations can now point at a chunk of the user's document as well as at a web
page (ADR-0015): a ``kind``, the document, the chunk, and the page, with the
URL columns nullable and a check constraint that each kind carries what it
needs. Existing rows are web citations and backfill through the server
default.

Hand-edited: the ``citation_kind`` enum is created and dropped explicitly,
and the check constraint is added (autogenerate does not compare them).

The downgrade **deletes document citations**: the old schema cannot represent
a citation without a URL. The answers that cited them keep their text; their
``[D1]`` markers render as plain text.

Revision ID: a49d12d3e4cc
Revises: 59742e5860d8
Create Date: 2026-10-20 13:21:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a49d12d3e4cc"
down_revision: str | None = "59742e5860d8"
branch_labels: str | None = None
depends_on: str | None = None

citation_kind = postgresql.ENUM("web", "document", name="citation_kind", create_type=False)

KIND_FIELDS = (
    "(kind = 'web' AND url IS NOT NULL AND display_url IS NOT NULL) OR "
    "(kind = 'document' AND document_id IS NOT NULL "
    "AND document_chunk_id IS NOT NULL AND page IS NOT NULL)"
)


def upgrade() -> None:
    """Upgrade schema."""
    citation_kind.create(op.get_bind(), checkfirst=False)
    op.add_column(
        "citations",
        sa.Column("kind", citation_kind, server_default="web", nullable=False),
    )
    op.add_column("citations", sa.Column("document_id", sa.UUID(), nullable=True))
    op.add_column("citations", sa.Column("document_chunk_id", sa.UUID(), nullable=True))
    op.add_column("citations", sa.Column("page", sa.Integer(), nullable=True))
    op.alter_column("citations", "url", existing_type=sa.Text(), nullable=True)
    op.alter_column("citations", "display_url", existing_type=sa.String(length=100), nullable=True)
    op.create_foreign_key(
        "citations_document_id_fkey",
        "citations",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "citations_document_chunk_id_fkey",
        "citations",
        "document_chunks",
        ["document_chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_citations_document", "citations", ["document_id"])
    op.create_index("ix_citations_document_chunk", "citations", ["document_chunk_id"])
    op.create_check_constraint("ck_citations_kind_fields", "citations", KIND_FIELDS)


def downgrade() -> None:
    """Downgrade schema. Deletes document citations; see the module docstring."""
    op.execute("DELETE FROM citations WHERE kind = 'document'")
    op.drop_constraint("ck_citations_kind_fields", "citations", type_="check")
    op.drop_index("ix_citations_document_chunk", table_name="citations")
    op.drop_index("ix_citations_document", table_name="citations")
    op.drop_constraint("citations_document_chunk_id_fkey", "citations", type_="foreignkey")
    op.drop_constraint("citations_document_id_fkey", "citations", type_="foreignkey")
    op.alter_column("citations", "display_url", existing_type=sa.String(length=100), nullable=False)
    op.alter_column("citations", "url", existing_type=sa.Text(), nullable=False)
    op.drop_column("citations", "page")
    op.drop_column("citations", "document_chunk_id")
    op.drop_column("citations", "document_id")
    op.drop_column("citations", "kind")
    citation_kind.drop(op.get_bind(), checkfirst=False)
