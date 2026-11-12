"""Users' own LLM API keys (ADR-0020).

One row per user and provider: the key, encrypted with
:class:`app.core.crypto.SecretBox` under the owner's id, and the model last
chosen for that provider. At most one row per user is *active*: the model the
user's answers come from. None active means the server's model.

Only ``key_hint`` (the key's last four characters) is ever shown back, so a
user can tell which key is saved without the API returning it.

``provider`` is the preset id from :mod:`app.providers.llm_catalog`. It is
not constrained in the database: presets are code and change more often than
the schema. A row naming a preset that no longer exists is ignored.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LLMCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The model chosen for this provider, remembered while another is active.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_hint: Mapped[str] = mapped_column(String(8), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # When a call with this key last succeeded at save time.
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_llm_credentials_user_id_provider"),
        # The database's guarantee that a user has one active model at most,
        # however two requests interleave.
        Index(
            "uq_llm_credentials_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the key, not even encrypted: reprs end up in tracebacks.
        return f"<LLMCredential user={self.user_id} provider={self.provider}>"
