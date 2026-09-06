"""Authentication endpoints.

Handlers here do three things only: read validated input, call the service,
and translate the result into an HTTP response (including cookie handling).
Every security decision lives in ``app.services.auth``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import (
    AuthServiceDep,
    ClientContextDep,
    CurrentUserDep,
    OriginVerified,
    SettingsDep,
    rate_limit,
)
from app.core.config import Settings
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
)
from app.schemas.user import UserRead
from app.services.auth import IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "refresh_token"


def _set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    """Attach the refresh token as a cookie the page's JavaScript cannot read.

    - ``HttpOnly``: XSS in the app cannot exfiltrate it.
    - ``SameSite=Lax``: it is not sent on cross-site POSTs, blocking CSRF.
    - ``Secure``: off only in development, where http would drop the cookie.
    - ``path``: scoped to /auth, so it is not attached to ordinary API calls.
    """
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path=settings.auth_cookie_path,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path=settings.auth_cookie_path,
    )


def _token_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=UserRead.model_validate(tokens.user),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[rate_limit("register", "rate_limit_register")],
)
async def register(
    payload: RegisterRequest,
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
    client: ClientContextDep,
) -> TokenResponse:
    tokens = await service.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        client=client,
    )
    _set_refresh_cookie(response, settings, tokens.refresh_token)
    return _token_response(tokens)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[rate_limit("login", "rate_limit_login")],
)
async def login(
    payload: LoginRequest,
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
    client: ClientContextDep,
) -> TokenResponse:
    tokens = await service.login(
        email=payload.email,
        password=payload.password,
        client=client,
    )
    _set_refresh_cookie(response, settings, tokens.refresh_token)
    return _token_response(tokens)


@router.post("/refresh", response_model=TokenResponse, dependencies=[OriginVerified])
async def refresh(
    request: Request,
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
    client: ClientContextDep,
) -> TokenResponse:
    """Exchange the refresh cookie for a new access token and a rotated cookie."""
    tokens = await service.refresh(
        refresh_token=request.cookies.get(REFRESH_COOKIE_NAME) or "",
        client=client,
    )
    _set_refresh_cookie(response, settings, tokens.refresh_token)
    return _token_response(tokens)


@router.post("/logout", response_model=MessageResponse, dependencies=[OriginVerified])
async def logout(
    request: Request,
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
) -> MessageResponse:
    await service.logout(refresh_token=request.cookies.get(REFRESH_COOKIE_NAME))
    _clear_refresh_cookie(response, settings)
    return MessageResponse(detail="Signed out.")


@router.post("/logout-all", response_model=MessageResponse, dependencies=[OriginVerified])
async def logout_all(
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
    current_user: CurrentUserDep,
) -> MessageResponse:
    """Revoke every session for the caller, on every device."""
    await service.logout_all(user_id=current_user.id)
    _clear_refresh_cookie(response, settings)
    return MessageResponse(detail="Signed out of all sessions.")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[rate_limit("forgot_password", "rate_limit_forgot_password")],
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    service: AuthServiceDep,
) -> MessageResponse:
    """Always returns the same acknowledgement.

    A different status, body, or timing for a known versus unknown address
    would turn this endpoint into an account-enumeration oracle.
    """
    await service.request_password_reset(email=payload.email)
    return MessageResponse(
        detail="If an account exists for that email, a reset link has been sent."
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    response: Response,
    settings: SettingsDep,
    service: AuthServiceDep,
) -> MessageResponse:
    await service.reset_password(token=payload.token, new_password=payload.password)
    # The reset revoked every session, so the browser's cookie is now dead.
    _clear_refresh_cookie(response, settings)
    return MessageResponse(detail="Password updated. Please sign in again.")
