"""Request and response models for the auth endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead

# Long passphrases are encouraged; the ceiling only bounds hashing work an
# unauthenticated caller can demand. Kept in sync with Settings by a test.
Password = Annotated[str, Field(min_length=12, max_length=1024)]
DisplayName = Annotated[str, Field(min_length=1, max_length=100)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    display_name: DisplayName


class LoginRequest(BaseModel):
    email: EmailStr
    password: Password


class TokenResponse(BaseModel):
    """The access token only.

    The refresh token is never in a response body — it goes out as an
    HttpOnly cookie so that script running in the page cannot read it.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - a scheme name, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds.")
    user: UserRead


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    password: Password


class VerifyEmailRequest(BaseModel):
    # A signed token, longer than an opaque one; the ceiling bounds parsing.
    token: str = Field(min_length=16, max_length=1024)


class MessageResponse(BaseModel):
    """Deliberately uninformative acknowledgement.

    Used where a precise answer would disclose whether an account exists.
    """

    detail: str
