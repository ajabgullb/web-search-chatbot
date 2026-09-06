"""Opaque keyset cursors.

Offset pagination is wrong for a feed that changes while it is being read: a
row inserted above the current page shifts everything down, so page two repeats
an item page one already showed. A keyset cursor names the *last row seen* and
asks for rows strictly after it, which is stable under concurrent writes.

Cursors are base64url of the sort key so clients treat them as opaque. That is
encoding, not encryption — it hides nothing secret, it only stops clients
building their own cursors and freezing an internal ordering into the contract.
"""

from __future__ import annotations

import base64
import binascii

# A unit separator cannot appear in a timestamp, a UUID, or an integer, so no
# escaping is needed for any sort key this application uses.
_SEPARATOR = "\x1f"


class InvalidCursorError(ValueError):
    """The cursor was not produced by :func:`encode_cursor`."""


def encode_cursor(*parts: str) -> str:
    raw = _SEPARATOR.join(parts).encode("utf-8")
    # Padding is dropped so the cursor survives a URL without escaping.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, fields: int) -> tuple[str, ...]:
    """Decode a cursor, or raise :class:`InvalidCursorError`.

    Callers map the exception to a 400. A malformed cursor is a client bug,
    never a reason to silently fall back to the first page — that would loop a
    paginating client forever.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("The cursor is not valid.") from exc

    parts = tuple(raw.split(_SEPARATOR))
    if len(parts) != fields:
        raise InvalidCursorError("The cursor is not valid.")
    return parts
