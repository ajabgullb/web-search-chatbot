"""Aggregates every ``/api/v1`` router.

Later phases add conversations, messages, and runs here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, health, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
