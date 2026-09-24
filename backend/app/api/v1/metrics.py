"""Prometheus scrape endpoint.

Metrics reveal traffic, error rates, and which dependencies are failing:
useful to an attacker, so production never serves them to the public. With
``METRICS_TOKEN`` set, a scraper must present it as a bearer token. In
production without one, the endpoint does not exist (404) and scrapes are
expected over an isolated network path instead. Development serves it
openly.

Left out of the OpenAPI schema: it is an operator interface, not part of the
product API the frontend client is generated from.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.api.dependencies import DatabaseDep, SettingsDep
from app.api.errors import AuthenticationRequired, NotFound
from app.core import telemetry

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, settings: SettingsDep, database: DatabaseDep) -> Response:
    token = settings.metrics_token
    if token:
        supplied = request.headers.get("authorization", "")
        # Constant-time: a plain == would leak, byte by byte, how much of a
        # guess was right.
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            raise AuthenticationRequired("A valid metrics token is required.")
    elif settings.is_production:
        raise NotFound
    body, content_type = telemetry.render(database)
    return Response(content=body, media_type=content_type)
