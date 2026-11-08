"""The MCP endpoint: checks every request, then hands it to the SDK (ADR-0019).

Mounted at ``/api/v1/mcp`` as Streamable HTTP in **stateless** mode. Each
request is complete in itself, so any API replica can serve any request and
nothing is kept in process memory between them, the same property the REST
API has. That also means every request is authenticated, not only the first.

In order, before the SDK sees a byte of the body:

1. ``Origin``. The MCP specification requires servers to validate it, to stop
   a web page from talking to a local server through DNS rebinding. Present
   and not the frontend's origin is a 403, as on the cookie routes.
2. The bearer token. Only a personal access token is accepted. The browser's
   JWT is refused, so the page's short-lived session cannot be lifted into a
   long-lived client. Every way a token can be wrong gets the same 401.
3. Rate limits. Per token for every request, and per client IP for failed
   token checks.

The SDK's own OAuth resource-server support is not used: it would advertise
an authorization server, and this application does not run one. A client
told to start an OAuth flow that cannot succeed fails more confusingly than
one told its token is wrong.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.routing import Match, Route
from starlette.types import Receive, Scope, Send

from app.api.errors import AuthenticationRequired, OriginNotAllowed, RateLimited, error_response
from app.api.mcp.server import PRINCIPAL_SCOPE_KEY, build_mcp_server
from app.core import telemetry
from app.core.config import Settings
from app.core.logging import bind_log_context
from app.core.rate_limit import RateLimiter
from app.db.session import Database
from app.services.access_tokens import AccessTokenService, Principal

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
_WWW_AUTHENTICATE = 'Bearer realm="mcp"'


class McpEndpoint:
    """A plain ASGI app: authenticate, then delegate to the SDK's transport."""

    def __init__(self, app: FastAPI, session_manager: StreamableHTTPSessionManager) -> None:
        self._app = app
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = self._app.state
        settings: Settings = state.settings
        request = Request(scope)

        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.cors_origins:
            telemetry.record_mcp_refused("origin")
            await error_response(OriginNotAllowed())(scope, receive, send)
            return

        token = _bearer_token(request)
        principal = await self._authenticate(state.database, settings, token)
        if principal is None:
            refusal = await _refuse_unauthenticated(
                request, state.rate_limiter, settings, presented=token is not None
            )
            await refusal(scope, receive, send)
            return

        decision = await state.rate_limiter.hit(
            f"mcp:{principal.token_id}",
            limit=settings.rate_limit_mcp_requests,
            window_seconds=settings.rate_limit_mcp_window_seconds,
        )
        if not decision.allowed:
            telemetry.record_rate_limited("mcp")
            telemetry.record_mcp_refused("rate_limited")
            await error_response(_rate_limited(decision.retry_after_seconds))(scope, receive, send)
            return

        # For the tools, and for the request log line the middleware writes.
        scope[PRINCIPAL_SCOPE_KEY] = principal
        scope.setdefault("state", {})["user_ref"] = principal.user_ref
        bind_log_context(user=principal.user_ref, token_id=str(principal.token_id))
        await self.session_manager.handle_request(scope, receive, send)

    async def _authenticate(
        self, database: Database, settings: Settings, token: str | None
    ) -> Principal | None:
        if not token:
            return None
        async with database.open_session() as session:
            return await AccessTokenService(session, settings).authenticate(token)


async def _refuse_unauthenticated(
    request: Request, limiter: RateLimiter, settings: Settings, *, presented: bool
) -> JSONResponse:
    """The 401, or a 429 once this client IP has failed too often."""
    client_ip = request.client.host if request.client else "unknown"
    decision = await limiter.hit(
        f"mcp_auth_failures:{client_ip}",
        limit=settings.rate_limit_mcp_auth_failures,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not decision.allowed:
        telemetry.record_rate_limited("mcp_auth_failures")
        telemetry.record_mcp_refused("rate_limited")
        return error_response(_rate_limited(decision.retry_after_seconds))
    telemetry.record_mcp_refused("unauthenticated")
    # RFC 6750: no error attribute when no credentials were sent at all.
    challenge = f'{_WWW_AUTHENTICATE}, error="invalid_token"' if presented else _WWW_AUTHENTICATE
    return error_response(
        AuthenticationRequired(
            "A valid personal access token is required. Create one under AI clients "
            "in the app, and send it as 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": challenge},
        )
    )


def _rate_limited(retry_after: int) -> RateLimited:
    return RateLimited(
        headers={"Retry-After": str(retry_after)}, details={"retry_after_seconds": retry_after}
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None


class _McpRoute(Route):
    """A route that labels its scope the way FastAPI's routes do.

    The request middleware reads ``scope["route"]`` for the metrics label;
    without this the endpoint would be counted as "unmatched".
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is not Match.NONE:
            child_scope["route"] = self
        return match, child_scope


def mount_mcp(app: FastAPI, settings: Settings) -> McpEndpoint:
    """Build the server and add its route. The session manager runs in the lifespan."""
    server: MCPServer = build_mcp_server(app, settings)
    # Called for its side effect of creating the session manager; the
    # Starlette app it returns is not used, because the endpoint above wraps
    # the manager directly.
    server.streamable_http_app(
        stateless_http=True,
        # Server-sent events, so ask can report progress while it runs.
        json_response=False,
        # Host and Origin checks are the endpoint's job (see the docstring).
        # The SDK's DNS-rebinding check would need every public host name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        max_request_body_size=settings.max_request_body_bytes,
    )
    endpoint = McpEndpoint(app, server.session_manager)
    app.router.routes.append(
        _McpRoute(
            f"{settings.api_v1_prefix}{MCP_PATH}",
            endpoint,
            methods=["GET", "POST", "DELETE"],
            include_in_schema=False,
        )
    )
    return endpoint
