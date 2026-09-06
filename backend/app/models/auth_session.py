"""Refresh-token session.

One row per issued refresh token. Rotation creates a new row and marks the old
one replaced, so the table is an audit trail of a login's whole lifetime, not
just its current state.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AuthSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 of the opaque token. The token itself is never stored, so a
    # database leak yields nothing a caller can present.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Every token descended from one login shares a family. Presenting an
    # already-rotated token revokes the entire family, which is what turns
    # a stolen token into a detectable event rather than a silent takeover.
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="SET NULL"),
        default=None,
    )
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    ip_fingerprint: Mapped[str | None] = mapped_column(String(32), default=None)

    __table_args__ = (
        # Listing a user's live sessions, and revoking a family on reuse.
        Index("ix_auth_sessions_user_id_revoked_at", "user_id", "revoked_at"),
        Index("ix_auth_sessions_family_id", "family_id"),
    )
