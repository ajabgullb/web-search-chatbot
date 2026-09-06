# API

Base path: `/api/v1`. Interactive docs at `/api/v1/docs`; the machine-readable
schema is committed at [`../openapi.json`](../openapi.json) and regenerated with
`make openapi`.

Status: reflects Phase 2. Conversations, messages, and runs arrive in Phases 4
and 6 — see [`PLAN.md`](../PLAN.md).

## Conventions

- Requests and responses are Pydantic models. ORM objects are never returned.
- Cursor pagination, never offset.
- Errors use one stable envelope (Phase 2):

  ```json
  {
    "error": {
      "code": "conversation_not_found",
      "message": "Conversation not found.",
      "request_id": "...",
      "details": null
    }
  }
  ```

- Ownership failures return `404`, never `403`, so a guessed UUID cannot reveal
  that another user's object exists.

## Endpoints

### `GET /api/v1/health/live`

Liveness. Always `200` while the process is running.

```json
{ "status": "ok" }
```

### `GET /api/v1/health/ready`

Readiness. `200` when every required dependency answers, otherwise `503` with
the same body shape.

```json
{ "status": "ready", "checks": { "database": true } }
```

## Authentication

Send the access token as `Authorization: Bearer <token>`. The refresh token is
never in a response body — it is set as an `HttpOnly` cookie scoped to
`/api/v1/auth`, so requests to those routes must be made with credentials.

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| POST | `/auth/register` | Create an account and sign in | `201` |
| POST | `/auth/login` | Sign in | `200` |
| POST | `/auth/refresh` | Rotate the refresh cookie, get a new access token | `200` |
| POST | `/auth/logout` | Revoke the current session | `200` |
| POST | `/auth/logout-all` | Revoke every session (requires bearer token) | `200` |
| POST | `/auth/forgot-password` | Request a reset link | `202` |
| POST | `/auth/reset-password` | Consume a reset token | `200` |
| GET | `/users/me` | The signed-in account | `200` |

`POST /auth/register` and `/auth/login` return:

```json
{
  "access_token": "...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "...", "email": "...", "display_name": "...",
    "is_active": true, "created_at": "..."
  }
}
```

### Error codes

| Code | Status | Meaning |
| --- | --- | --- |
| `validation_failed` | 422 | Body failed validation; `details.fields` lists the failures |
| `authentication_required` | 401 | Missing, invalid, or expired access token |
| `invalid_credentials` | 401 | Wrong email or password — identical for an unknown account |
| `invalid_token` | 401 | Refresh or reset token missing, expired, or already used |
| `account_inactive` | 403 | The account has been deactivated |
| `origin_not_allowed` | 403 | Cross-site request to a cookie-authenticated route |
| `email_already_registered` | 409 | Registration conflict |
| `rate_limited` | 429 | Too many attempts; see the `Retry-After` header |
| `internal_error` | 500 | Unexpected failure; quote `request_id` |
