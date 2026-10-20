"""Turning uploaded PDF bytes into page text, safely.

Each parse runs in its own child process (``pdf_worker.py``), started from a
forkserver:

- **Killable.** ``ProcessPoolExecutor`` cannot stop one stuck task, so a PDF
  crafted to hang the parser would hold a pool worker forever. A process of
  its own is killed at the deadline.
- **Cheap to start.** The forkserver preloads ``pypdf`` once; each parse is a
  fork of that small, single-threaded server, not a fresh interpreter, and not
  a fork of the API process with its event loop and threads.
- **Bounded.** At most ``DOCUMENT_PARSE_WORKERS`` parses run at once. One more
  is refused with a retryable error instead of queueing in memory.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from app.core import telemetry
from app.core.config import Settings
from app.services.pdf_worker import (
    ParsedPdf,
    ParseErrorCode,
    ParseFailure,
    ParseLimits,
    run_in_child,
)

logger = logging.getLogger(__name__)

# The worker's own imports: nothing from the app beyond it and chunking.
_FORKSERVER_PRELOAD = ["app.services.pdf_worker"]

# After a kill, how long to wait for the process to be reaped.
_REAP_SECONDS = 5.0

ChildTarget = Callable[[bytes, ParseLimits, Connection], None]


class PdfError(Exception):
    """The PDF cannot be used. ``code`` is the stable API error code."""

    def __init__(self, code: ParseErrorCode, *, limit: str | None = None) -> None:
        super().__init__(code)
        self.code: ParseErrorCode = code
        self.limit = limit


class PdfParserBusy(Exception):
    """Every parse slot is taken. Retryable."""


class PdfParser:
    def __init__(
        self,
        *,
        limits: ParseLimits,
        timeout_seconds: float,
        max_concurrent: int,
        # Tests substitute a target that hangs or crashes on purpose.
        target: ChildTarget = run_in_child,
    ) -> None:
        self._limits = limits
        self._timeout = timeout_seconds
        self._target = target
        self._slots = asyncio.Semaphore(max_concurrent)
        self._context: Any = multiprocessing.get_context("forkserver")
        self._context.set_forkserver_preload(_FORKSERVER_PRELOAD)

    @classmethod
    def from_settings(cls, settings: Settings) -> PdfParser:
        return cls(
            limits=ParseLimits(
                max_pages=settings.max_document_pages,
                max_chars=settings.max_document_chars,
                memory_bytes=settings.document_parse_memory_mb * 1024 * 1024,
            ),
            timeout_seconds=settings.document_parse_timeout_seconds,
            max_concurrent=settings.document_parse_workers,
        )

    async def parse(self, data: bytes) -> ParsedPdf:
        """The page count and chunks. Raises :class:`PdfError` or :class:`PdfParserBusy`."""
        if self._slots.locked():
            raise PdfParserBusy
        async with self._slots:
            started = time.monotonic()
            # Waiting on a pipe blocks, so it happens in a thread. The slot
            # bounds those threads too.
            result = await asyncio.to_thread(self._parse_blocking, data)
            elapsed = time.monotonic() - started
            outcome = result.code if isinstance(result, ParseFailure) else "ok"
            telemetry.record_pdf_parse(outcome, elapsed)
            logger.info(
                "pdf_parsed", extra={"duration_ms": round(elapsed * 1000), "outcome": outcome}
            )
        if isinstance(result, ParseFailure):
            raise PdfError(result.code, limit=result.limit)
        return result

    def _parse_blocking(self, data: bytes) -> ParsedPdf | ParseFailure:
        receive, send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=self._target, args=(data, self._limits, send), daemon=True
        )
        process.start()
        # Drop the parent's copy of the sending end, so a child that dies
        # without replying shows up as EOF rather than as silence.
        send.close()
        try:
            if not receive.poll(self._timeout):
                logger.warning("pdf_parse_timeout", extra={"timeout_seconds": self._timeout})
                return ParseFailure("pdf_too_complex")
            try:
                reply: ParsedPdf | ParseFailure = receive.recv()
            except EOFError:
                # Killed by the kernel or crashed in native code.
                logger.warning("pdf_parse_crashed", extra={"exit_code": process.exitcode})
                return ParseFailure("pdf_too_complex")
            return reply
        finally:
            receive.close()
            if process.is_alive():
                process.kill()
            process.join(_REAP_SECONDS)
