"""Personal access tokens for MCP clients (ADR-0019).

Managed from the browser with the ordinary access JWT. A personal access token
cannot call these routes, or any other REST route: it works only at the MCP
endpoint, so a leaked token can never mint more tokens or revoke the user's.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import AccessTokenServiceDep, CurrentUserDep
from app.schemas.access_tokens import (
    AccessTokenCreate,
    AccessTokenCreated,
    AccessTokenList,
    AccessTokenRead,
)

router = APIRouter(prefix="/access-tokens", tags=["access tokens"])


@router.get("", response_model=AccessTokenList)
async def list_access_tokens(
    current_user: CurrentUserDep, service: AccessTokenServiceDep
) -> AccessTokenList:
    """The caller's tokens that have not been revoked, expired ones included."""
    tokens = await service.list(user=current_user)
    return AccessTokenList(items=[AccessTokenRead.model_validate(token) for token in tokens])


@router.post("", response_model=AccessTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_access_token(
    payload: AccessTokenCreate, current_user: CurrentUserDep, service: AccessTokenServiceDep
) -> AccessTokenCreated:
    """Mint a token. The response is the only time the token itself is shown."""
    token, raw = await service.create(user=current_user, payload=payload)
    read = AccessTokenRead.model_validate(token)
    return AccessTokenCreated(**read.model_dump(), token=raw)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_access_token(
    token_id: uuid.UUID, current_user: CurrentUserDep, service: AccessTokenServiceDep
) -> None:
    """Revoke a token. Every client using it is refused from its next request."""
    await service.revoke(user=current_user, token_id=token_id)
