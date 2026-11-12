"""Encryption at rest for secrets the application must read back (ADR-0020).

Passwords, refresh tokens, and access tokens are *hashed*: the server only
ever compares them. A user's LLM API key is different: each run must send it
to the provider, so it has to be recoverable, and it is encrypted instead.

AES-256-GCM, an authenticated cipher: a ciphertext that has been altered, or
that was encrypted under another key, fails to open rather than decrypting to
garbage. The 256-bit key is derived from ``CREDENTIAL_ENCRYPTION_KEY`` with
HKDF, so the setting can be any long random string rather than exactly 32
bytes of binary.

Every ciphertext is bound to *associated data*, the id of the row's owner.
Copying one user's encrypted key onto another user's row (a SQL injection
elsewhere, a botched restore) then fails to decrypt instead of quietly
handing the second user the first one's key.

Layout: ``version (1 byte) || nonce (12 bytes) || ciphertext+tag``. The
version byte leaves room to rotate the key or change the scheme later without
guessing which rows use which.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VERSION = 1
_NONCE_BYTES = 12
# Domain separation: the same input secret used for anything else in future
# derives an unrelated key.
_HKDF_INFO = b"web-search-chatbot/llm-credential/v1"


class DecryptionError(Exception):
    """The ciphertext is corrupt, foreign, or from a different key."""


class SecretBox:
    def __init__(self, secret: str) -> None:
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
            secret.encode()
        )
        self._aead = AESGCM(key)

    def seal(self, plaintext: str, *, associated_data: bytes) -> bytes:
        # A fresh random nonce per encryption. GCM's one hard rule is never to
        # reuse a nonce under the same key; at 96 random bits the chance of a
        # collision is negligible for any realistic number of saved keys.
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode(), associated_data)
        return bytes([_VERSION]) + nonce + sealed

    def open(self, blob: bytes, *, associated_data: bytes) -> str:
        if len(blob) < 1 + _NONCE_BYTES or blob[0] != _VERSION:
            raise DecryptionError("unknown ciphertext format")
        nonce = blob[1 : 1 + _NONCE_BYTES]
        try:
            plaintext = self._aead.decrypt(nonce, blob[1 + _NONCE_BYTES :], associated_data)
        except InvalidTag as exc:
            raise DecryptionError("ciphertext failed authentication") from exc
        return plaintext.decode()
