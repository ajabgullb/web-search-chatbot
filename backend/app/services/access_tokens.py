"""Personal access tokens: minting, listing, revoking, and checking (ADR-0019).

A token is ``wsc_pat_`` followed by 32 random bytes, URL-safe. The fixed
prefix does two jobs. It lets the MCP endpoint refuse anything else, a JWT
included, before touching the database. And it makes a leaked token easy to
recognise: a secret scanner can match it with one pattern.

Only the SHA-256 is stored, as with refresh tokens. A token has at least 256
bits of entropy, so a fast hash is enough to make a leaked table useless, and
it doubles as the indexed lookup key.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AccessTokenLimitReached, AccessTokenNotFound, ValidationFailed
from app.core.config import Settings
from app.core.logging import user_ref
from app.core.security import hash_opaque_token
from app.models.personal_access_token import PersonalAccessToken, TokenScope
from app.models.user import User
from app.repositories.personal_access_tokens import PersonalAccessTokenRepository
from app.repositories.users import UserRepository
from app.schemas.access_tokens import AccessTokenCreate

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "wsc_pat_"  # noqa: S105 - a public marker, not a secret
# The prefix plus four random characters: enough to tell a user's handful of
# tokens apart, far too little to help anyone guess the rest.
DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 4
# token_urlsafe(32) is 43 characters. Anything much longer is not ours, and
# refusing it early keeps junk out of the hash.
_MAX_TOKEN_LENGTH = 128


def generate_access_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_access_token_shaped(value: str) -> bool:
    """Cheap check before any database work: right prefix, plausible length."""
    return value.startswith(TOKEN_PREFIX) and len(TOKEN_PREFIX) < len(value) <= _MAX_TOKEN_LENGTH


def normalize_token_name(raw: str) -> str:
    return " ".join(raw.split())


@dataclass(frozen=True, slots=True)
class Principal:
    """Who an MCP request acts for, and what it may do."""

    user_id: uuid.UUID
    token_id: uuid.UUID
    scopes: frozenset[TokenScope]

    def allows(self, scope: TokenScope) -> bool:
        return scope in self.scopes

    @property
    def user_ref(self) -> str:
        return user_ref(self.user_id)


class AccessTokenService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._tokens = PersonalAccessTokenRepository(session)
        self._users = UserRepository(session)

    async def create(
        self, *, user: User, payload: AccessTokenCreate
    ) -> tuple[PersonalAccessToken, str]:
        """Mint a token. Returns the row and the token, which is never stored."""
        name = normalize_token_name(payload.name)
        if not name:
            raise ValidationFailed(
                "The token needs a name.",
                details={"fields": [{"field": "name", "reason": "must not be blank"}]},
            )
        max_days = self._settings.access_token_max_ttl_days
        if payload.expires_in_days > max_days:
            raise ValidationFailed(
                f"A token can last at most {max_days} days.",
                details={
                    "fields": [
                        {"field": "expires_in_days", "reason": f"must be at most {max_days}"}
                    ],
                    "max_days": max_days,
                },
            )

        now = datetime.now(UTC)
        limit = self._settings.max_access_tokens_per_user
        if await self._tokens.count_live_for_user(user_id=user.id, now=now) >= limit:
            raise AccessTokenLimitReached(
                f"An account can have at most {limit} access tokens. Revoke one first.",
                details={"limit": limit},
            )

        raw = generate_access_token()
        token = self._tokens.add(
            PersonalAccessToken(
                user_id=user.id,
                name=name,
                token_hash=hash_opaque_token(raw),
                token_prefix=raw[:DISPLAY_PREFIX_LENGTH],
                # Sorted and unique, so the stored list compares equal to any
                # other spelling of the same choice.
                scopes=sorted({scope.value for scope in payload.scopes}),
                expires_at=now + timedelta(days=payload.expires_in_days),
            )
        )
        await self._session.commit()
        await self._session.refresh(token)
        logger.info(
            "access_token_created",
            extra={
                "user": user_ref(user.id),
                "token_id": str(token.id),
                "scopes": token.scopes,
                "expires_in_days": payload.expires_in_days,
            },
        )
        return token, raw

    async def list(self, *, user: User) -> list[PersonalAccessToken]:
        return list(await self._tokens.list_for_user(user_id=user.id))

    async def revoke(self, *, user: User, token_id: uuid.UUID) -> None:
        revoked = await self._tokens.revoke(
            user_id=user.id, token_id=token_id, at=datetime.now(UTC)
        )
        if not revoked:
            # Someone else's token, a revoked one, or one that never existed:
            # all the same 404, so ids cannot be probed.
            raise AccessTokenNotFound
        await self._session.commit()
        logger.info(
            "access_token_revoked", extra={"user": user_ref(user.id), "token_id": str(token_id)}
        )

    async def authenticate(self, raw: str) -> Principal | None:
        """The principal a presented token acts for, or None.

        None covers every way a token can be wrong: unknown, revoked, expired,
        or belonging to an account that has been deactivated. The caller
        answers all of them alike, so a response never says which.
        """
        if not is_access_token_shaped(raw):
            return None
        token = await self._tokens.get_by_token_hash(hash_opaque_token(raw))
        now = datetime.now(UTC)
        if token is None or token.revoked_at is not None or token.expires_at <= now:
            return None
        user = await self._users.get_by_id(token.user_id)
        if user is None or not user.is_active:
            return None
        await self._tokens.touch(token_id=token.id, at=now)
        await self._session.commit()
        return Principal(
            user_id=user.id,
            token_id=token.id,
            scopes=frozenset(TokenScope(scope) for scope in token.scopes),
        )
