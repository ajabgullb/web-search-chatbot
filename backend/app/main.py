"""FastAPI application factory.

Shared clients are constructed once in the lifespan and closed on shutdown, so
no request path ever builds an engine, HTTP client, or provider client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.checkpointer import checkpointer_for
from app.agent.context import AgentLimits
from app.agent.graph import build_graph
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.rate_limit import build_rate_limiter
from app.core.redis import build_redis_client
from app.core.run_lock import build_run_lock
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
    app.state.run_manager = RunManager(
        app.state.database,
        app.state.run_lock,
        abandon_grace_seconds=settings.run_abandon_grace_seconds,
    )

    logger.info(
        "application startup: environment=%s redis=%s",
        settings.environment,
        "enabled" if app.state.redis is not None else "disabled",
    )
    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(checkpointer_for(settings))
        app.state.agent_graph = build_graph(checkpointer)
        try:
            yield
        finally:
            # Runs first: each records how it ended and releases its run lock,
            # which needs the database, the checkpointer, and Redis still open.
            await app.state.run_manager.shutdown()
            await app.state.search_provider.aclose()
            if app.state.redis is not None:
                await app.state.redis.aclose()
            await app.state.database.dispose()
            logger.info("application shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url=f"{settings.api_v1_prefix}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Middleware runs outermost-last, so the request id is established before
    # anything else and is available to every handler and log record.
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
