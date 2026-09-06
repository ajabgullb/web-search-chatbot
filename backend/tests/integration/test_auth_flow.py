"""Registration, login, identity, and logout."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.integration.conftest import REGISTER_PASSWORD, auth_header, register

pytestmark = pytest.mark.integration


async def test_register_returns_tokens_and_sets_refresh_cookie(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "New.User@Example.COM",
            "password": REGISTER_PASSWORD,
            "display_name": "New User",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    # Email is normalized on the way in, so casing cannot create duplicates.
    assert body["user"]["email"] == "new.user@example.com"
    # The refresh token must never appear in a body a script could read.
    assert "refresh_token" not in body

    # Cookie attribute names are case-insensitive per RFC 6265, and the
    # casing a server emits is not part of the contract.
    cookie = response.headers["set-cookie"].lower()
    assert "refresh_token=" in cookie
    assert "httponly" in cookie, "script must not be able to read the refresh token"
    assert "secure" in cookie, "must not be sent over plain http"
    assert "samesite=lax" in cookie, "must not ride along on cross-site requests"
    assert "path=/api/v1/auth" in cookie, "must not be attached to ordinary API calls"


async def test_password_hash_is_never_exposed(client: AsyncClient) -> None:
    tokens = await register(client)
    response = await client.get("/api/v1/users/me", headers=auth_header(tokens))

    assert response.status_code == 200
    assert set(response.json()) == {"id", "email", "display_name", "is_active", "created_at"}


async def test_duplicate_email_is_rejected_regardless_of_case(client: AsyncClient) -> None:
    await register(client, email="dupe@example.com")

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "DUPE@example.com",
            "password": REGISTER_PASSWORD,
            "display_name": "Impostor",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


async def test_login_succeeds_and_rejects_a_wrong_password(client: AsyncClient) -> None:
    await register(client, email="login@example.com")

    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": REGISTER_PASSWORD},
    )
    assert ok.status_code == 200

    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "wrong-password-entirely"},
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"


async def test_unknown_account_and_wrong_password_are_indistinguishable(
    client: AsyncClient,
) -> None:
    """Login must not become an account-enumeration oracle."""
    await register(client, email="known@example.com")

    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": REGISTER_PASSWORD},
    )
    wrong = await client.post(
        "/api/v1/auth/login",
        json={"email": "known@example.com", "password": "wrong-password-entirely"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


async def test_me_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me")

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "authentication_required"
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_me_rejects_a_tampered_token(client: AsyncClient) -> None:
    tokens = await register(client)
    forged = tokens["access_token"][:-4] + "AAAA"

    response = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    await register(client, email="bye@example.com")

    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    # The cookie is cleared, so the browser has nothing left to refresh with.
    refresh = await client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 401
    assert refresh.json()["error"]["code"] == "invalid_token"


async def test_logout_all_revokes_every_device(client: AsyncClient) -> None:
    tokens = await register(client, email="multi@example.com")

    # A second "device": its own client, hence its own cookie jar.
    other = AsyncClient(transport=client._transport, base_url="https://testserver")
    login = await other.post(
        "/api/v1/auth/login",
        json={"email": "multi@example.com", "password": REGISTER_PASSWORD},
    )
    assert login.status_code == 200

    response = await client.post("/api/v1/auth/logout-all", headers=auth_header(tokens))
    assert response.status_code == 200

    # The other device's refresh token is dead even though it never logged out.
    stranded = await other.post("/api/v1/auth/refresh")
    assert stranded.status_code == 401
    await other.aclose()
