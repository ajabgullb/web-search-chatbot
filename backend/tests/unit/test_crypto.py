"""Encryption at rest for users' API keys (ADR-0020)."""

from __future__ import annotations

import pytest

from app.core.crypto import DecryptionError, SecretBox

SECRET = "a-test-encryption-secret-that-is-long-enough"
OWNER = b"owner-a"


def test_a_sealed_value_opens_again() -> None:
    box = SecretBox(SECRET)
    assert box.open(box.seal("sk-abc", associated_data=OWNER), associated_data=OWNER) == "sk-abc"


def test_the_plaintext_is_not_in_the_ciphertext() -> None:
    assert b"sk-very-recognisable" not in SecretBox(SECRET).seal(
        "sk-very-recognisable", associated_data=OWNER
    )


def test_each_seal_uses_a_fresh_nonce() -> None:
    """Reusing a GCM nonce under one key is catastrophic; equal output would show it."""
    box = SecretBox(SECRET)
    assert box.seal("same", associated_data=OWNER) != box.seal("same", associated_data=OWNER)


def test_another_owner_cannot_open_it() -> None:
    box = SecretBox(SECRET)
    sealed = box.seal("sk-abc", associated_data=OWNER)
    with pytest.raises(DecryptionError):
        box.open(sealed, associated_data=b"owner-b")


def test_another_key_cannot_open_it() -> None:
    sealed = SecretBox(SECRET).seal("sk-abc", associated_data=OWNER)
    with pytest.raises(DecryptionError):
        SecretBox(SECRET + "-rotated").open(sealed, associated_data=OWNER)


@pytest.mark.parametrize("position", [0, 5, -1])
def test_any_tampering_is_detected(position: int) -> None:
    box = SecretBox(SECRET)
    sealed = bytearray(box.seal("sk-abc", associated_data=OWNER))
    sealed[position] ^= 0x01
    with pytest.raises(DecryptionError):
        box.open(bytes(sealed), associated_data=OWNER)


def test_a_truncated_blob_is_refused() -> None:
    with pytest.raises(DecryptionError):
        SecretBox(SECRET).open(b"\x01short", associated_data=OWNER)
