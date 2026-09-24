"""FastAPI application factory.

Shared clients are constructed once in the lifespan and closed on shutdown, so
no request path ever builds an engine, HTTP client, or provider client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app.agent.checkpointer import checkpointer_for
from app.agent.context import AgentLimits
from app.agent.graph import build_graph
from app.agent.tracing import build_tracing
from app.api.errors import register_exception_handlers
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.rate_limit import build_rate_limiter
from app.core.redis import build_redis_client
from app.core.run_lock import build_run_lock
from app.core.run_signals import RedisCancelSignal
from app.db.session import Database
from app.providers.email import build_email_sender
from app.providers.llm import build_llm_provider
from app.providers.search import build_search_provider
from app.services.run_executor import RunManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.database = Database(settings)
    app.state.email_sender = build_email_sender(settings)
    app.state.redis = build_redis_client(settings)
    app.state.rate_limiter = build_rate_limiter(app.state.redis)
    app.state.run_lock = build_run_lock(
        app.state.redis, lease_seconds=settings.run_lock_lease_seconds
    )

    # Provider clients and the compiled graph are built once here. A graph
    # compiled per request would rebuild its node table on every message, and a
    # provider client per request would open a new connection pool each time.
    app.state.llm_provider = build_llm_provider(settings)
    app.state.search_provider = build_search_provider(settings)
    app.state.agent_limits = AgentLimits.from_settings(settings)
    app.state.tracing = build_tracing(settings)
    cancel_signal = RedisCancelSignal(app.state.redis) if app.state.redis is not None else None
    app.state.run_manager = RunManager(
        app.state.database,
        app.state.run_lock,
        cancel_signal=cancel_signal,
        abandon_grace_seconds=settings.run_abandon_grace_seconds,
    )
    app.state.cancel_signal = cancel_signal

    logger.info(
        "app_started",
        extra={"environment": settings.environment.value, "redis": app.state.redis is not None},
    )
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(checkpointer_for(settings))
        app.state.agent_graph = build_graph(checkpointer)
        listener = (
            asyncio.create_task(
                cancel_signal.listen(app.state.run_manager.on_cancel_signal),
                name="run-cancel-listener",
            )
            if cancel_signal is not None
            else None
        )
        try:
            yield
        finally:
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            # Runs first: each records how it ended and releases its run lock,
            # which needs the database, the checkpointer, and Redis still open.
            # Read at shutdown, not startup, so an operator (or a test) can
            # change it on a running app.
            await app.state.run_manager.shutdown(
                drain_seconds=app.state.settings.shutdown_drain_seconds
            )
            await app.state.search_provider.aclose()
            await app.state.tracing.aclose()
            if app.state.redis is not None:
                await app.state.redis.aclose()
            await app.state.database.dispose()
            logger.info("app_stopped")


def _load_settings() -> Settings:
    """Settings, or a clean exit naming what is wrong.

    Pydantic's own error includes a repr of the input it rejected, which here
    is the process environment: printed to a log at startup, it would leak
    fragments of real secrets. Only the messages are shown, never the values.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error["loc"]).upper()
            message = error["msg"].removeprefix("Value error, ")
            lines.append(message if not field else f"{field}: {message}")
        raise SystemExit("Invalid configuration.\n" + "\n".join(lines)) from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or _load_settings()
    setup_logging(settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json" if settings.expose_api_docs else None,
        docs_url=f"{settings.api_v1_prefix}/docs" if settings.expose_api_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Middleware runs outermost-last, so the request id is established before
    # anything else and is available to every handler and log record. The
    # body limit is innermost: its 413 still passes through CORS (so a browser
    # can read it) and gets the security headers.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    # Explicit origins only: credentialed requests may never use a wildcard.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
