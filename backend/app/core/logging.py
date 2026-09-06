"""Logging setup.

Phase 1 keeps this to a readable console format. Phase 8 replaces the formatter
with structured JSON plus redaction, without changing this entry point.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str) -> None:
    """Configure the root logger idempotently."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Re-running (reload, tests) must not stack duplicate handlers.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
    root.addHandler(handler)

    # Uvicorn installs its own handlers; let records propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
