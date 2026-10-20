"""PDF text extraction, run inside a disposable child process.

Everything here executes in a process of its own (see ``pdf.py``), never in
the API process. A PDF is attacker-controlled input to a complex parser: a
crafted file can loop, or allocate until the machine swaps. In a child it can
be killed when it overruns its deadline, and an address-space limit makes it
fail with ``MemoryError`` long before it endangers the host.

Chunking runs here too, for the same reason: it is CPU-bound work on
attacker-sized input (up to 500,000 characters). Measured on the event loop
it cost 19 ms per maximum-size document; here it costs the API nothing.

Imports are limited to the standard library, ``pypdf``, and the pure chunking
module on purpose. The forkserver preloads this module, so every child starts
with exactly this and nothing else: no settings, no secrets, no open sockets.
"""

from __future__ import annotations

import io
import resource
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Literal

from app.services.chunking import chunk_pages

# Codes, not messages: the parent maps them onto API errors. Kept as plain
# strings so the reply pickles without importing anything from the app.
ParseErrorCode = Literal[
    "pdf_invalid",
    "pdf_encrypted",
    "pdf_no_text",
    # Over a limit the user can act on (split the document): pages or text.
    "pdf_too_large",
    # Over a limit the user cannot act on: memory, time, decompression size.
    "pdf_too_complex",
]


@dataclass(frozen=True, slots=True)
class ParseLimits:
    max_pages: int
    max_chars: int
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class ParsedPdf:
    """What a readable PDF yields: its page count and its chunks as (page, text)."""

    page_count: int
    chunks: list[tuple[int, str]]


@dataclass(frozen=True, slots=True)
class ParseFailure:
    code: ParseErrorCode
    # Which limit was exceeded, for ``pdf_too_large``: "pages" or "characters".
    limit: str | None = None


def extract_pages(data: bytes, limits: ParseLimits) -> list[str] | ParseFailure:
    """Return each page's text, in order, or the reason there is none.

    Stops reading as soon as a limit is exceeded, so an oversized document
    costs the time to discover that it is oversized and no more.
    """
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, LimitReachedError, PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many PDFs are "encrypted" only to restrict printing or copying,
            # with an empty password for reading. Those are readable. A real
            # password is not something the user can give us here.
            try:
                if not reader.decrypt(""):
                    return ParseFailure("pdf_encrypted")
            except Exception:
                # pypdf needs the optional ``cryptography`` package for AES. A
                # PDF it cannot decrypt is, to the user, a PDF we cannot read.
                return ParseFailure("pdf_encrypted")

        page_count = len(reader.pages)
        if page_count == 0:
            return ParseFailure("pdf_invalid")
        if page_count > limits.max_pages:
            return ParseFailure("pdf_too_large", "pages")

        pages: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > limits.max_chars:
                return ParseFailure("pdf_too_large", "characters")
            pages.append(text)
    except FileNotDecryptedError:
        return ParseFailure("pdf_encrypted")
    except LimitReachedError:
        # pypdf's own guard against decompression bombs.
        return ParseFailure("pdf_too_complex")
    except MemoryError:
        return ParseFailure("pdf_too_complex")
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        # pypdf surfaces malformed structure as a mix of its own errors and
        # plain Python ones. Either way the file is not a PDF we can read.
        return ParseFailure("pdf_invalid")

    if not any(text.strip() for text in pages):
        # A scan: pages that are pictures of text. There is no OCR here.
        return ParseFailure("pdf_no_text")
    return pages


def parse_document(data: bytes, limits: ParseLimits) -> ParsedPdf | ParseFailure:
    """Pages, then chunks: everything an upload needs from the file."""
    pages = extract_pages(data, limits)
    if isinstance(pages, ParseFailure):
        return pages
    chunks = chunk_pages(pages)
    if not chunks:
        # Text that normalizes to nothing: whitespace and control characters.
        return ParseFailure("pdf_no_text")
    return ParsedPdf(page_count=len(pages), chunks=[(c.page, c.content) for c in chunks])


def run_in_child(data: bytes, limits: ParseLimits, reply: Connection) -> None:
    """Child-process entry point: cap memory, parse, send one reply, exit."""
    try:
        # Address space, not resident memory: it is the only limit the kernel
        # enforces at allocation time, so an allocation over it fails inside
        # Python (as MemoryError) instead of the process being OOM-killed.
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        result: ParsedPdf | ParseFailure = parse_document(data, limits)
    except MemoryError:
        result = ParseFailure("pdf_too_complex")
    try:
        reply.send(result)
    finally:
        reply.close()
