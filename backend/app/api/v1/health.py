"""Liveness and readiness probes.

Liveness answers "is this process alive?" and must never fail because an
external provider is down — otherwise an LLM outage would trigger a restart
loop. Readiness answers "should this process receive traffic?" and does check
the dependencies the API cannot serve without.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.dependencies import DatabaseDep
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(database: DatabaseDep, response: Response) -> ReadinessResponse:
    checks = {"database": await database.check_connection()}
    is_ready = all(checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if is_ready else "not_ready", checks=checks)
