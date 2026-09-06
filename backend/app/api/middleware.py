"""Cross-cutting HTTP middleware: request correlation and security headers."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import Settings
from app.core.request_context import set_request_id

RequestHandler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-ID"

# Swagger UI pulls its bundle from a CDN, so the API-wide `default-src 'none'`
# policy would break the docs page. The docs are a development affordance;
# every other response gets the strict policy.
_CSP_EXEMPT_SUFFIXES = ("/docs", "/docs/oauth2-redirect", "/openapi.json")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Adopt the caller's request id or mint one, and echo it back.

    Accepting an inbound id lets a trace span a proxy, the frontend, and the
    API. It is treated as an opaque label: it is length-capped and never
    interpolated into anything but a header and a log field.
    """

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming[:64] if incoming else str(uuid.uuid4())

        set_request_id(request_id)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
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
