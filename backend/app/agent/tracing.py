"""Optional LangSmith tracing, privacy-safe by construction.

Off unless ``LANGSMITH_TRACING=true``. When on, each run's graph execution is
sent to LangSmith with its structure, timings, token counts, errors, tags, and
version metadata, but **not its inputs or outputs**: prompts, conversation
turns, search results, and answers are replaced with empty payloads before
anything leaves the process.

Tracing is explicit: a tracer callback is attached to each run's config.
LangChain also has an implicit mode, which traces every call, full content
included, whenever ``LANGSMITH_TRACING`` is set in the process environment.
Docker's ``env_file`` puts every ``.env`` line into that environment, so that
mode would bypass the hiding below. :func:`build_tracing` therefore switches
it off unconditionally and owns the decision itself.

A LangSmith outage never affects a run. The client sends from a background
thread, and a failed upload is logged by the SDK, not raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith import utils as langsmith_utils

from app.agent.prompts import PROMPT_VERSION
from app.core.config import Settings

logger = logging.getLogger(__name__)

# Bump when nodes, edges, or state change shape, so traces (and evaluations)
# from before and after a graph change can be told apart.
GRAPH_VERSION = "2026-09-24.1"

_IMPLICIT_TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


def hide_payload(_payload: dict[str, Any]) -> dict[str, Any]:
    """Replace a run's inputs or outputs before upload. Nothing survives."""
    return {}


@dataclass(frozen=True, slots=True)
class TraceConfig:
    """What one run adds to its LangGraph config."""

    callbacks: list[BaseCallbackHandler] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


class Tracing:
    def __init__(self, client: Client | None, *, project: str, environment: str) -> None:
        self._client = client
        self._project = project
        self._environment = environment

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def for_run(self, *, run_id: uuid.UUID) -> TraceConfig:
        # Identifiers only. The conversation id and user are left out: a trace
        # store is one more place user activity would be linkable.
        metadata = {
            "environment": self._environment,
            "prompt_version": PROMPT_VERSION,
            "graph_version": GRAPH_VERSION,
            "run_id": str(run_id),
        }
        tags = [f"env:{self._environment}", f"prompt:{PROMPT_VERSION}"]
        callbacks: list[BaseCallbackHandler] = []
        if self._client is not None:
            callbacks.append(LangChainTracer(project_name=self._project, client=self._client))
        return TraceConfig(callbacks=callbacks, tags=tags, metadata=metadata)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        """Send what is queued, but never hold up shutdown for LangSmith.

        The flush runs on a daemon thread, not the event loop's executor: the
        loop waits for executor threads when it closes, so a flush retrying
        against an unreachable endpoint would delay process exit long after
        this method had given up on it.
        """
        client = self._client
        if client is None:
            return
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def flush() -> None:
            try:
                client.flush()
            except Exception:
                logger.warning("tracing_flush_failed")
            finally:
                # The loop may already be closing if the flush outlived us.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(done.set)

        threading.Thread(target=flush, name="langsmith-flush", daemon=True).start()
        try:
            await asyncio.wait_for(done.wait(), timeout_seconds)
        except TimeoutError:
            logger.warning("tracing_flush_abandoned", extra={"timeout_seconds": timeout_seconds})


def build_tracing(settings: Settings) -> Tracing:
    _disable_implicit_tracing()
    environment = settings.environment.value
    if not settings.langsmith_tracing:
        return Tracing(None, project=settings.langsmith_project, environment=environment)
    client = Client(
        api_key=settings.langsmith_api_key,
        api_url=settings.langsmith_endpoint or None,
        hide_inputs=hide_payload,
        hide_outputs=hide_payload,
    )
    logger.info("tracing_enabled", extra={"project": settings.langsmith_project})
    return Tracing(client, project=settings.langsmith_project, environment=environment)


def _disable_implicit_tracing() -> None:
    # The one place the application writes the process environment: these
    # variables are read by LangChain itself, not through Settings.
    for name in _IMPLICIT_TRACING_VARIABLES:
        os.environ[name] = "false"
    # The SDK caches environment lookups; drop anything read before now. Looked
    # up defensively: the cache is an implementation detail of the SDK.
    clear_cache = getattr(langsmith_utils.get_env_var, "cache_clear", None)
    if clear_cache is not None:
        clear_cache()
