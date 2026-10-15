"""Password hashing, token signing, and opaque-token handling."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Environment, Settings
from app.core.security import (
    TokenError,
    create_access_token,
    create_email_verification_token,
    decode_access_token,
    decode_email_verification_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    normalize_email,
    verify_password,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.test, jwt_secret="unit-test-secret-value-long-enough-for-hs256"
    )


# --- Passwords ---------------------------------------------------------------


async def test_hashing_is_salted_and_verifiable() -> None:
    password = "correct-horse-battery-staple"
    first = await hash_password(password)
    second = await hash_password(password)

    assert first != second, "each hash must carry its own salt"
    assert await verify_password(password, first)
    assert await verify_password(password, second)


async def test_hash_is_argon2id() -> None:
    assert (await hash_password("correct-horse-battery-staple")).startswith("$argon2id$")


async def test_wrong_password_returns_false_rather_than_raising() -> None:
    stored = await hash_password("correct-horse-battery-staple")
    assert await verify_password("nope", stored) is False


async def test_verify_tolerates_a_corrupt_stored_hash() -> None:
    """A malformed row must fail the login, not crash the endpoint."""
    assert await verify_password("anything", "not-a-hash") is False


async def test_long_passphrases_are_not_truncated() -> None:
    """Unlike bcrypt, Argon2 has no 72-byte cut-off; prove it."""
    base = "a" * 200
    stored = await hash_password(base)
    assert await verify_password(base, stored)
    assert await verify_password(base + "b", stored) is False


async def test_hashing_does_not_block_the_event_loop() -> None:
    """Other requests keep being served while a password is checked.

    A ticker that wants to run every 5 ms measures the longest gap it sees
    while verifications run. On the loop, each verification stalls it for
    the full Argon2 cost (100 ms or more); off the loop, the gaps stay near
    the tick interval.
    """
    stored = await hash_password("correct-horse-battery-staple")
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    for _ in range(3):
        await verify_password("correct-horse-battery-staple", stored)
    done.set()
    await task

    assert len(gaps) > 3, "the loop should have ticked while hashing ran"
    # Generous for a loaded CI machine, and still far below one Argon2 run.
    assert max(gaps) < 0.06


# --- Access tokens -----------------------------------------------------------


def test_round_trip_carries_user_and_session(settings: Settings) -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    token, expires_in = create_access_token(settings, user_id=user_id, session_id=session_id)
    claims = decode_access_token(settings, token)

    assert claims.user_id == user_id
    assert claims.session_id == session_id
    assert expires_in == settings.access_token_ttl_minutes * 60


def test_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    other = Settings(
        environment=Environment.test, jwt_secret="a-different-secret-value-long-enough-for-hs256"
    )
    token, _ = create_access_token(other, user_id=uuid.uuid4(), session_id=uuid.uuid4())

    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_expired_token_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(
        settings,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        now=datetime.now(UTC) - timedelta(hours=2),
    )

    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_wrong_audience_or_issuer_is_rejected(settings: Settings) -> None:
    foreign = Settings(
        environment=Environment.test,
        jwt_secret=settings.jwt_secret,
        jwt_audience="some-other-api",
    )
    token, _ = create_access_token(foreign, user_id=uuid.uuid4(), session_id=uuid.uuid4())

    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_unsigned_token_is_rejected(settings: Settings) -> None:
    """The classic `alg: none` attack, blocked by pinning the algorithm."""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "typ": "access",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        decode_access_token(settings, forged)


def test_a_non_access_token_type_is_rejected(settings: Settings) -> None:
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "typ": "refresh",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenError):
        decode_access_token(settings, token)


# --- Opaque tokens -----------------------------------------------------------


def test_opaque_tokens_are_unique_and_long() -> None:
    tokens = {generate_opaque_token() for _ in range(200)}

    assert len(tokens) == 200
    assert all(len(token) >= 43 for token in tokens)


def test_opaque_hash_is_deterministic_and_hides_the_token() -> None:
    token = generate_opaque_token()
    digest = hash_opaque_token(token)

    assert digest == hash_opaque_token(token), "lookup depends on determinism"
    assert token not in digest
    assert len(digest) == 64


# --- Email normalization -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  User@Example.COM ", "user@example.com"),
        ("ALL@CAPS.IO", "all@caps.io"),
        ("already@normal.com", "already@normal.com"),
    ],
)
def test_email_normalization(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


# --- Email verification tokens -----------------------------------------------


def test_verification_token_round_trip(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_email_verification_token(settings, user_id=user_id, email="a@example.com")

    claims = decode_email_verification_token(settings, token)

    assert claims.user_id == user_id
    assert claims.email == "a@example.com"


def test_an_access_token_is_not_a_verification_token(settings: Settings) -> None:
    """Same secret, different purpose: each decoder insists on its own type."""
    access, _ = create_access_token(settings, user_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_email_verification_token(settings, access)


def test_a_verification_token_is_not_an_access_token(settings: Settings) -> None:
    """Otherwise the emailed link would be a working credential for the API."""
    token = create_email_verification_token(settings, user_id=uuid.uuid4(), email="a@example.com")
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_an_expired_verification_token_is_rejected(settings: Settings) -> None:
    issued = datetime.now(UTC) - timedelta(hours=settings.email_verification_ttl_hours + 1)
    token = create_email_verification_token(
        settings, user_id=uuid.uuid4(), email="a@example.com", now=issued
    )
    with pytest.raises(TokenError):
        decode_email_verification_token(settings, token)
