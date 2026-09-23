"""Aggregates every ``/api/v1`` router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.errors import ERROR_RESPONSES
from app.api.v1 import auth, conversations, health, runs, users

# Every route can fail with the standard envelope, so it is declared once here
# rather than repeated on each endpoint.
api_router = APIRouter(responses=ERROR_RESPONSES)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(conversations.router)
api_router.include_router(runs.router)
