"""Shared FastAPI dependencies.

Long-lived clients live on ``app.state`` and are built once in the lifespan.
Dependencies read them from the request rather than creating their own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.params import Depends as DependsMarker
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    AccountInactive,
    AuthenticationRequired,
    OriginNotAllowed,
    RateLimited,
)
from app.core import telemetry
from app.core.config import Settings
from app.core.logging import bind_log_context, user_ref
from app.core.rate_limit import RateLimiter
from app.core.run_lock import RunLock
from app.core.security import TokenError, decode_access_token
from app.db.session import Database
from app.models.user import User
from app.providers.email import EmailSender
from app.repositories.users import UserRepository
from app.services.auth import AuthService, ClientContext
from app.services.conversations import ConversationService
from app.services.run_executor import AgentRuntime, RunManager
from app.services.runs import RunService

# auto_error=False so a missing header raises our envelope, not FastAPI's.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    """Return the settings this app was *built* with.

    Reading the cached ``get_settings()`` singleton here instead would silently
    ignore the instance passed to ``create_app``, so an app configured one way
    could behave another. Every dependency resolves configuration from the
    running app, never from the process environment.
    """
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_redis(request: Request) -> Redis | None:
    """The shared Redis client, or None when Redis is not configured."""
    redis: Redis | None = request.app.state.redis
    return redis


def get_email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async for session in database.session():
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[Redis | None, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def get_client_context(request: Request) -> ClientContext:
    """Request metadata recorded against a session, for audit purposes."""
    return ClientContext(
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )


ClientContextDep = Annotated[ClientContext, Depends(get_client_context)]


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    email_sender: EmailSenderDep,
) -> AuthService:
    return AuthService(session, settings, email_sender)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """Resolve the bearer token to a live, active user.

    The token is re-checked against the database on every request rather than
    trusted on its claims alone, so a deactivated account loses access
    immediately instead of at the end of the access token's lifetime.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationRequired

    try:
        claims = decode_access_token(settings, credentials.credentials)
    except TokenError as exc:
        raise AuthenticationRequired("The access token is invalid or has expired.") from exc

    user = await UserRepository(session).get_by_id(claims.user_id)
    if user is None:
        raise AuthenticationRequired
    if not user.is_active:
        raise AccountInactive

    reference = user_ref(user.id)
    bind_log_context(user=reference)
    request.state.user_ref = reference
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def get_conversation_service(session: SessionDep) -> ConversationService:
    return ConversationService(session)


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


def get_run_lock(request: Request) -> RunLock:
    run_lock: RunLock = request.app.state.run_lock
    return run_lock


RunLockDep = Annotated[RunLock, Depends(get_run_lock)]


def get_run_service(session: SessionDep, settings: SettingsDep, run_lock: RunLockDep) -> RunService:
    return RunService(session, settings, run_lock)


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


def get_run_manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.run_manager
    return manager


RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]


def get_agent_runtime(request: Request) -> AgentRuntime:
    """Resolved per request from app.state, so a test can swap a provider on
    a running app and the next run uses it."""
    state = request.app.state
    return AgentRuntime(
        graph=state.agent_graph,
        llm=state.llm_provider,
        search=state.search_provider,
        limits=state.agent_limits,
    )


AgentRuntimeDep = Annotated[AgentRuntime, Depends(get_agent_runtime)]


async def enforce_run_rate_limit(
    current_user: CurrentUserDep, settings: SettingsDep, limiter: RateLimiterDep
) -> None:
    """Per-user limit on starting runs — the path that spends provider money."""
    decision = await limiter.hit(
        f"runs:{current_user.id}",
        limit=settings.rate_limit_runs,
        window_seconds=settings.rate_limit_runs_window_seconds,
    )
    if not decision.allowed:
        telemetry.record_rate_limited("runs")
        raise RateLimited(
            headers={"Retry-After": str(decision.retry_after_seconds)},
            details={"retry_after_seconds": decision.retry_after_seconds},
        )


def verify_origin(request: Request, settings: SettingsDep) -> None:
    """Reject cross-site state-changing requests on cookie-authenticated routes.

    ``SameSite=Lax`` already blocks the cross-site POST that CSRF depends on,
    but it is a browser-side control with historical gaps. Checking ``Origin``
    server-side is a cheap second lock on the same door.

    Requests with no ``Origin`` at all (curl, a server-side client) are
    allowed: they are not browsers, so they cannot be a confused deputy.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    if origin not in settings.cors_origins:
        raise OriginNotAllowed


OriginVerified = Depends(verify_origin)


def rate_limit(bucket: str, limit_attribute: str) -> DependsMarker:
    """Build a dependency that rate-limits a route by client IP.

    The limit is read from settings at request time rather than captured at
    import time, so tests can raise or lower it without re-importing routes.
    """

    async def dependency(
        request: Request,
        settings: SettingsDep,
        limiter: RateLimiterDep,
    ) -> None:
        client_ip = request.client.host if request.client else "unknown"
        limit: int = getattr(settings, limit_attribute)
        decision = await limiter.hit(
            f"{bucket}:{client_ip}",
            limit=limit,
            window_seconds=settings.rate_limit_window_seconds,
        )
        if not decision.allowed:
            telemetry.record_rate_limited(bucket)
            raise RateLimited(
                headers={"Retry-After": str(decision.retry_after_seconds)},
                details={"retry_after_seconds": decision.retry_after_seconds},
            )

    # fastapi.Depends is untyped; the marker class it constructs is what
    # `dependencies=[...]` actually expects.
    return DependsMarker(dependency)
