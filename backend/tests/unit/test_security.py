"""Password hashing, token signing, and opaque-token handling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Environment, Settings
from app.core.security import (
    TokenError,
    create_access_token,
    decode_access_token,
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


def test_hashing_is_salted_and_verifiable() -> None:
    password = "correct-horse-battery-staple"
    first = hash_password(password)
    second = hash_password(password)

    assert first != second, "each hash must carry its own salt"
    assert verify_password(password, first)
    assert verify_password(password, second)


def test_hash_is_argon2id() -> None:
    assert hash_password("correct-horse-battery-staple").startswith("$argon2id$")


def test_wrong_password_returns_false_rather_than_raising() -> None:
    assert verify_password("nope", hash_password("correct-horse-battery-staple")) is False


def test_verify_tolerates_a_corrupt_stored_hash() -> None:
    """A malformed row must fail the login, not crash the endpoint."""
    assert verify_password("anything", "not-a-hash") is False


def test_long_passphrases_are_not_truncated() -> None:
    """Unlike bcrypt, Argon2 has no 72-byte cut-off; prove it."""
    base = "a" * 200
    assert verify_password(base, hash_password(base))
    assert verify_password(base + "b", hash_password(base)) is False


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
