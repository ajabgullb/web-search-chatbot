"""Authentication orchestration.

Route handlers stay thin by delegating here. This module owns the rules:
what counts as a valid login, how refresh rotation and reuse detection work,
and what a caller is allowed to learn from a failure.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidToken,
)
from app.core.config import Settings
from app.core.logging import user_ref
from app.core.security import (
    create_access_token,
    fingerprint_ip,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    normalize_email,
    verify_dummy_password,
    verify_password,
)
from app.models.auth_session import AuthSession
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.providers.email import EmailPayload, EmailSender
from app.repositories.auth_sessions import AuthSessionRepository
from app.repositories.password_reset_tokens import PasswordResetTokenRepository
from app.repositories.users import UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Non-identifying request metadata attached to a session."""

    user_agent: str | None = None
    ip: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    user: User


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        email_sender: EmailSender,
    ) -> None:
        self._session = session
        self._settings = settings
        self._email = email_sender
        self._users = UserRepository(session)
        self._sessions = AuthSessionRepository(session)
        self._reset_tokens = PasswordResetTokenRepository(session)

    # --- Registration and login ---------------------------------------------

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        client: ClientContext,
    ) -> IssuedTokens:
        normalized = normalize_email(email)

        user = User(
            email=normalized,
            password_hash=hash_password(password),
            display_name=display_name.strip(),
        )
        self._users.add(user)
        try:
            # Rely on the unique index rather than a pre-check: two concurrent
            # registrations of the same address would both pass a check and
            # only the constraint can actually decide.
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise EmailAlreadyRegistered from exc

        tokens = await self._start_session(user, client=client)
        await self._session.commit()
        logger.info("user_registered", extra={"user": user_ref(user.id)})
        return tokens

    async def login(self, *, email: str, password: str, client: ClientContext) -> IssuedTokens:
        user = await self._users.get_by_email(normalize_email(email))

        if user is None:
            # Spend the same time as a real verification so response latency
            # cannot be used to discover which addresses are registered.
            verify_dummy_password(password)
            raise InvalidCredentials

        if not verify_password(password, user.password_hash):
            raise InvalidCredentials

        # An inactive account gets the same answer as a wrong password: its
        # status is not something an anonymous caller should be able to probe.
        if not user.is_active:
            raise InvalidCredentials

        tokens = await self._start_session(user, client=client)
        await self._session.commit()
        logger.info("user_logged_in", extra={"user": user_ref(user.id)})
        return tokens

    # --- Refresh rotation ----------------------------------------------------

    async def refresh(self, *, refresh_token: str, client: ClientContext) -> IssuedTokens:
        """Rotate a refresh token, detecting reuse.

        Rotation means the presented token is always retired. If a token that
        has *already* been retired shows up, there are two possibilities —
        theft, or a client replaying — and neither is safe to serve, so the
        whole family is revoked and every device must log in again.
        """
        now = datetime.now(UTC)
        stored = await self._sessions.get_by_token_hash(hash_opaque_token(refresh_token))

        if stored is None:
            raise InvalidToken

        if stored.revoked_at is not None:
            revoked = await self._sessions.revoke_family(stored.family_id, at=now)
            await self._session.commit()
            logger.warning(
                "refresh_token_reuse_detected",
                extra={
                    "user": user_ref(stored.user_id),
                    "family_id": stored.family_id,
                    "sessions_revoked": revoked,
                },
            )
            raise InvalidToken

        if stored.expires_at <= now:
            raise InvalidToken

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise InvalidToken

        rotated = self._issue_session(
            user,
            family_id=stored.family_id,
            client=client,
            now=now,
        )
        await self._session.flush()

        stored.revoked_at = now
        stored.replaced_by_id = rotated.session.id

        await self._session.commit()
        return self._tokens_for(user, rotated)

    # --- Logout --------------------------------------------------------------

    async def logout(self, *, refresh_token: str | None) -> None:
        """Revoke the current session.

        Always succeeds: a caller trying to log out with a token that is
        already gone has got what they wanted.
        """
        if not refresh_token:
            return

        stored = await self._sessions.get_by_token_hash(hash_opaque_token(refresh_token))
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = datetime.now(UTC)
            await self._session.commit()

    async def logout_all(self, *, user_id: uuid.UUID) -> int:
        revoked = await self._sessions.revoke_all_for_user(user_id, at=datetime.now(UTC))
        await self._session.commit()
        logger.info(
            "user_logged_out_all", extra={"user": user_ref(user_id), "sessions_revoked": revoked}
        )
        return revoked

    # --- Password reset ------------------------------------------------------

    async def request_password_reset(self, *, email: str) -> None:
        """Issue a reset token if the account exists.

        Returns None either way. The endpoint's response is identical for a
        known and an unknown address, so this flow cannot be used to test
        whether someone has an account.
        """
        now = datetime.now(UTC)
        user = await self._users.get_by_email(normalize_email(email))

        if user is None or not user.is_active:
            logger.info("password_reset_requested_for_unknown_account")
            return

        await self._reset_tokens.invalidate_outstanding_for_user(user.id, at=now)

        token = generate_opaque_token()
        self._reset_tokens.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_opaque_token(token),
                expires_at=now + timedelta(minutes=self._settings.password_reset_ttl_minutes),
            )
        )
        await self._session.commit()

        # A link, not a bare token: the frontend reset page reads the token
        # from the query string, so the user never copies it by hand.
        reset_url = f"{self._settings.frontend_origin}/reset-password?token={token}"
        await self._email.send(
            EmailPayload(
                to=user.email,
                subject=f"Reset your {self._settings.app_name} password",
                body=(
                    f"Hello {user.display_name},\n\n"
                    "Open the link below to choose a new password. It can be used once "
                    f"and expires in {self._settings.password_reset_ttl_minutes} minutes.\n\n"
                    f"{reset_url}\n\n"
                    "If you did not request this, you can safely ignore this email.\n"
                ),
            )
        )
        logger.info("password_reset_email_sent", extra={"user": user_ref(user.id)})

    async def reset_password(self, *, token: str, new_password: str) -> None:
        """Consume a reset token and set a new password.

        Every existing session is revoked: if the reset was triggered because
        the account was compromised, leaving the attacker's session alive
        would defeat the point.
        """
        now = datetime.now(UTC)
        stored = await self._reset_tokens.get_by_token_hash(hash_opaque_token(token))

        if stored is None or stored.used_at is not None or stored.expires_at <= now:
            raise InvalidToken

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise InvalidToken

        user.password_hash = hash_password(new_password)
        stored.used_at = now
        revoked = await self._sessions.revoke_all_for_user(user.id, at=now)

        await self._session.commit()
        logger.info(
            "password_reset_completed",
            extra={"user": user_ref(user.id), "sessions_revoked": revoked},
        )

    # --- Internals -----------------------------------------------------------

    @dataclass(frozen=True, slots=True)
    class _NewSession:
        session: AuthSession
        refresh_token: str

    def _issue_session(
        self,
        user: User,
        *,
        family_id: uuid.UUID,
        client: ClientContext,
        now: datetime,
    ) -> AuthService._NewSession:
        refresh_token = generate_opaque_token()
        auth_session = AuthSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_opaque_token(refresh_token),
            family_id=family_id,
            expires_at=now + timedelta(days=self._settings.refresh_token_ttl_days),
            user_agent=(client.user_agent or "")[:255] or None,
            ip_fingerprint=fingerprint_ip(client.ip),
        )
        self._sessions.add(auth_session)
        return AuthService._NewSession(session=auth_session, refresh_token=refresh_token)

    async def _start_session(self, user: User, *, client: ClientContext) -> IssuedTokens:
        """Begin a new token family — one per login, not per rotation."""
        now = datetime.now(UTC)
        issued = self._issue_session(user, family_id=uuid.uuid4(), client=client, now=now)
        await self._session.flush()
        return self._tokens_for(user, issued)

    def _tokens_for(self, user: User, issued: AuthService._NewSession) -> IssuedTokens:
        access_token, expires_in = create_access_token(
            self._settings,
            user_id=user.id,
            session_id=issued.session.id,
        )
        return IssuedTokens(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=issued.refresh_token,
            user=user,
        )
