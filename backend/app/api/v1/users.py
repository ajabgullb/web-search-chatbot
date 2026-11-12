"""Account endpoints for the signed-in user."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status

from app.api.dependencies import (
    CurrentUserDep,
    LLMCredentialServiceDep,
    enforce_llm_key_check_limit,
)
from app.providers.llm import LLMProvider
from app.providers.llm_catalog import PRESETS
from app.schemas.llm_credential import (
    PROVIDER_PATTERN,
    LLMCredentialRead,
    LLMCredentialWrite,
    LLMProviderRead,
    LLMSelectionRead,
    LLMSelectionWrite,
    LLMSettingsRead,
    ServerModelRead,
)
from app.schemas.user import UserRead
from app.services.llm_credentials import LLMCredentialService, choose

router = APIRouter(prefix="/users", tags=["users"])

ProviderPath = Annotated[str, Path(pattern=PROVIDER_PATTERN, description="A provider preset id.")]


@router.get("/me", response_model=UserRead)
async def read_current_user(current_user: CurrentUserDep) -> UserRead:
    return UserRead.model_validate(current_user)


# --- AI provider settings (ADR-0020) ------------------------------------------
# Bearer-authenticated like every route but the refresh cookie's, so no Origin
# check is needed. A personal access token cannot reach these: it works only
# at the MCP endpoint, so a leaked MCP token cannot read or replace a key.


def _server_model(request: Request) -> str | None:
    server: LLMProvider | None = request.app.state.llm_provider
    return server.model if server is not None else None


async def _settings(
    request: Request, user_id: uuid.UUID, service: LLMCredentialService
) -> LLMSettingsRead:
    credentials = await service.list(user_id=user_id)
    server_model = _server_model(request)
    selection = choose(credentials, server_model)
    return LLMSettingsRead(
        providers=[LLMProviderRead.build(preset) for preset in PRESETS.values()],
        credentials=[LLMCredentialRead.model_validate(credential) for credential in credentials],
        server_model=ServerModelRead(model=server_model) if server_model is not None else None,
        selection=(
            LLMSelectionRead(
                source=selection.source, provider=selection.provider, model=selection.model
            )
            if selection is not None
            else None
        ),
    )


@router.get("/me/llm-settings", response_model=LLMSettingsRead)
async def read_llm_settings(
    request: Request, current_user: CurrentUserDep, service: LLMCredentialServiceDep
) -> LLMSettingsRead:
    """The providers on offer, the caller's saved keys (described, never
    returned), the server's model, and which model answers next."""
    return await _settings(request, current_user.id, service)


@router.put(
    "/me/llm-credentials/{provider}",
    response_model=LLMSettingsRead,
    dependencies=[Depends(enforce_llm_key_check_limit)],
)
async def save_llm_credential(
    provider: ProviderPath,
    payload: LLMCredentialWrite,
    request: Request,
    current_user: CurrentUserDep,
    service: LLMCredentialServiceDep,
) -> LLMSettingsRead:
    """Save (or replace) the key for a provider, after one small call proves
    the key and model work, and switch to it. Nothing is saved on failure."""
    user_id = current_user.id
    await service.save_key(
        user_id=user_id,
        provider=provider,
        model=payload.model,
        api_key=payload.api_key.get_secret_value(),
    )
    return await _settings(request, user_id, service)


@router.delete("/me/llm-credentials/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_credential(
    provider: ProviderPath, current_user: CurrentUserDep, service: LLMCredentialServiceDep
) -> Response:
    """Forget the key for a provider. Safe to repeat."""
    await service.delete(user_id=current_user.id, provider=provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/me/llm-selection", response_model=LLMSettingsRead)
async def select_llm(
    payload: LLMSelectionWrite,
    request: Request,
    current_user: CurrentUserDep,
    service: LLMCredentialServiceDep,
) -> LLMSettingsRead:
    """Switch the model answers come from: one of the caller's saved keys
    with a model, or (both null) the app's default. Instant: no provider call."""
    user_id = current_user.id
    await service.select(
        user_id=user_id,
        provider=payload.provider,
        model=payload.model,
        server_model=_server_model(request),
    )
    return await _settings(request, user_id, service)
