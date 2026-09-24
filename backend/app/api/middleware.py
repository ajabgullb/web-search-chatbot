"""Cross-cutting HTTP middleware: request correlation and security headers."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import PayloadTooLarge, error_response
from app.core import telemetry
from app.core.config import Settings
from app.core.logging import clear_log_context
from app.core.request_context import set_request_id

logger = logging.getLogger("app.http")

RequestHandler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-ID"

# Swagger UI pulls its bundle from a CDN, so the API-wide `default-src 'none'`
# policy would break the docs page. The docs are a development affordance;
# every other response gets the strict policy.
_CSP_EXEMPT_SUFFIXES = ("/docs", "/docs/oauth2-redirect", "/openapi.json")


# Probes and scrapes arrive every few seconds; at INFO they would bury the
# requests anyone is looking for.
_QUIET_PATH_SUFFIXES = ("/health/live", "/health/ready", "/metrics")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Adopt the caller's request id or mint one, echo it back, and log the request.

    Accepting an inbound id lets a trace span a proxy, the frontend, and the
    API. It is treated as an opaque label: it is length-capped and never
    interpolated into anything but a header and a log field.
    """

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming[:64] if incoming else str(uuid.uuid4())

        clear_log_context()
        set_request_id(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id

        duration = time.perf_counter() - started
        path = request.url.path
        # The route template, not the path: one series per endpoint rather
        # than one per conversation id. Unmatched paths (scanners probing for
        # /wp-admin) share a single label.
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        telemetry.record_http(request.method, route, response.status_code, duration)
        level = logging.DEBUG if path.endswith(_QUIET_PATH_SUFFIXES) else logging.INFO
        # For a stream this is time to the first byte, not the whole stream;
        # a run's own duration is logged when it finishes.
        logger.log(
            level,
            "http_request",
            extra={
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 1),
                # Set by the auth dependency, which runs in a child context
                # this middleware cannot see.
                "user": getattr(request.state, "user_ref", None),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defence-in-depth headers on every response.

    The API returns JSON, never HTML, so it can afford a near-total lockdown:
    nothing should be loaded from it, and it should never be framed.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        # Responses carrying tokens or personal data must not sit in a shared
        # cache; the API has nothing worth caching by default.
        response.headers.setdefault("Cache-Control", "no-store")

        if not request.url.path.endswith(_CSP_EXEMPT_SUFFIXES):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )

        # HSTS is meaningless over http and is normally set at the edge proxy;
        # setting it here too means a direct-exposure deployment is still safe.
        if self._settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response


class BodySizeLimitMiddleware:
    """Refuse request bodies over ``MAX_REQUEST_BODY_BYTES`` before parsing them.

    Pure ASGI rather than ``BaseHTTPMiddleware``, because it must see the body
    as it arrives. Two cases:

    - A declared ``Content-Length`` over the limit is refused at once, and the
      body is never read.
    - A body without one (chunked transfer) is counted as it streams in, and
      reading stops the moment it passes the limit. The error is raised inside
      the application's body read. It must be Starlette's HTTPException:
      FastAPI re-raises that from a failed body read, but rewrites anything
      else into a generic 400. The normal handlers then build the envelope.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            await error_response(self._too_large())(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise StarletteHTTPException(413, PayloadTooLarge.message)
            return message

        await self._app(scope, limited_receive, send)

    def _too_large(self) -> PayloadTooLarge:
        return PayloadTooLarge(details={"max_bytes": self._max_bytes})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
