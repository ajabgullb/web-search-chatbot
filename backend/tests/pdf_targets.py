"""Child-process stand-ins for the PDF worker that misbehave on purpose.

A module of their own, importing almost nothing: the forkserver child imports
it to find the target, and should not have to load the test suite to do so.
"""

from __future__ import annotations

import os
import time
from multiprocessing.connection import Connection

from app.services.pdf_worker import ParseLimits


def hang(data: bytes, limits: ParseLimits, reply: Connection) -> None:
    """A parser stuck in a loop: never replies."""
    time.sleep(3600)


def crash(data: bytes, limits: ParseLimits, reply: Connection) -> None:
    """A parser killed by the kernel or crashing in native code: exits without replying."""
    os._exit(1)
