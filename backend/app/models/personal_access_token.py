"""Personal access tokens: how an MCP client acts for a user (ADR-0019).

A browser holds a 15-minute JWT in memory and a refresh cookie, and an MCP
client can use neither. So a user mints a long-lived, scoped, revocable token
for each client and pastes it into that client's configuration.

Stored like refresh tokens: only a SHA-256 of the token, so a database leak
yields nothing anyone can present. ``token_prefix`` is the first characters of
the token, kept so the user can tell their tokens apart in a list.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TokenScope(StrEnum):
    """What a token may do. Checked by every MCP tool, never by the client."""

    # List conversations, read their messages, search their documents.
    conversations_read = "conversations:read"
    # Start runs: spends the account's daily message allowance.
    runs_create = "runs:create"


class PersonalAccessToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "personal_access_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The user's label for the client it is pasted into ("Claude Desktop").
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    # Every token expires. A forgotten token in an old config file is the
    # usual way one leaks, and an expiry bounds how long that matters.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Updated at most once a minute (see the repository), so a busy client
    # does not write a row per request.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # Listing a user's tokens, and counting the live ones against the cap.
        Index("ix_personal_access_tokens_user_id_revoked_at", "user_id", "revoked_at"),
        # The application validates scopes; the database refuses anything
        # else too, so a bug cannot store a scope no tool checks for.
        CheckConstraint(
            "cardinality(scopes) > 0 "
            "AND scopes <@ ARRAY['conversations:read', 'runs:create']::varchar[]",
            name="ck_personal_access_tokens_scopes_known",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PersonalAccessToken id={self.id} user={self.user_id}>"
