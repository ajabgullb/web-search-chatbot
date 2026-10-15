"""Password hashing, access tokens, and opaque token handling.

Two deliberately different hashing strategies live here:

* **Passwords** are low-entropy and user-chosen, so they get Argon2id — slow
  and memory-hard by design, to make offline cracking expensive.
* **Refresh and reset tokens** are 256+ bits of CSPRNG output, so brute force
  is not the threat; the only requirement is that a database leak must not
  yield usable tokens. SHA-256 achieves that and, unlike Argon2, is fast
  enough to be an indexed lookup key.

Argon2 is slow on purpose (~100-300 ms of CPU), which is exactly why it must
not run on the event loop: one login would freeze every open answer stream in
the process for that long. The password functions are therefore async and run
the hash in a small dedicated thread pool. argon2-cffi releases the GIL while
it works, so the loop keeps serving other requests meanwhile.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

# argon2-cffi defaults to Argon2id with parameters tracking the RFC 9106
# recommendations. Kept explicit so a library default change is visible here.
_password_hasher = PasswordHasher()

# Each hash already spreads over ``parallelism`` (4) lanes and holds 64 MiB, so
# more workers add memory, not throughput. Two keeps peak hashing memory at
# 128 MiB however many logins arrive at once: the excess waits its turn in the
# queue instead of exhausting the container. Threads start lazily on first use.
_HASH_WORKERS = 2
_hash_executor = ThreadPoolExecutor(max_workers=_HASH_WORKERS, thread_name_prefix="argon2")

# A pre-computed hash of a value nobody can log in with. Verifying against it
# when the account does not exist keeps login timing flat, so response time
# cannot be used to enumerate registered emails.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


class TokenError(Exception):
    """An access token was absent, malformed, expired, or not ours."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The subset of JWT claims the application acts on."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def _verify_blocking(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_hash_executor, _password_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password, returning False rather than raising on mismatch."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_hash_executor, _verify_blocking, password, password_hash)


async def verify_dummy_password(password: str) -> None:
    """Burn the same work as a real verification for an unknown account."""
    await verify_password(password, _DUMMY_HASH)


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current Argon2 parameters."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_opaque_token() -> str:
    """A refresh or reset token: 48 random bytes, URL-safe."""
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    """Hash an opaque token for storage and lookup.

    Deterministic on purpose: the database column is unique and indexed, so a
    presented token can be found in one query.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Return a signed access JWT and its lifetime in seconds.

    ``session_id`` ties the access token to the refresh-token family, so a
    revoked session can be recognised before its short expiry elapses.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid.uuid4()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.access_token_ttl_minutes * 60


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    """Validate signature, issuer, audience, expiry, and token type."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            # Pinning the algorithm list prevents the classic `alg: none`
            # and HS/RS confusion attacks.
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("typ") != "access":
        raise TokenError("not an access token")

    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed claims") from exc


@dataclass(frozen=True, slots=True)
class EmailVerificationClaims:
    user_id: uuid.UUID
    email: str


def create_email_verification_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    email: str,
    now: datetime | None = None,
) -> str:
    """A signed, self-contained email verification token.

    Signed rather than stored, unlike reset tokens: it only proves control of
    an inbox, and using it twice is harmless, so there is nothing a database
    row would add. It names the address it was sent to, so it stops working
    if the account's email ever changes.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(hours=settings.email_verification_ttl_hours)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "typ": "email_verification",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_email_verification_token(settings: Settings, token: str) -> EmailVerificationClaims:
    """Validate a verification token. An access token is not one, and vice
    versa: each decoder insists on its own ``typ``."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("typ") != "email_verification":
        raise TokenError("not an email verification token")

    try:
        return EmailVerificationClaims(
            user_id=uuid.UUID(payload["sub"]), email=str(payload["email"])
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed claims") from exc


def normalize_email(email: str) -> str:
    """Casefold and trim so one address cannot register twice."""
    return email.strip().casefold()


def fingerprint_ip(ip: str | None) -> str | None:
    """Store a salted-per-secret digest instead of a raw client IP.

    Enough to spot a session hopping networks, without keeping personal data
    in a table that is dumped in every backup.
    """
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]
