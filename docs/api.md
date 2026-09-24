# API

Base path: `/api/v1`. Interactive docs at `/api/v1/docs`; the machine-readable
schema is committed at [`../openapi.json`](../openapi.json) and regenerated with
`make openapi`.

Status: reflects Phase 6 — authentication, conversations, messages, and
streaming runs. See [`PLAN.md`](../PLAN.md) for what comes next.

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
the same body shape. `redis` appears only when `REDIS_URL` is configured
(always in production).

```json
{ "status": "ready", "checks": { "database": true, "redis": true } }
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

### Conversations

All routes require a bearer token. Every query is constrained by the caller's
`user_id` in SQL, so another user's conversation is indistinguishable from one
that does not exist: both answer `404 conversation_not_found`, never `403`.

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| GET | `/conversations?cursor=&limit=` | Newest-first page of the caller's threads | `200` |
| POST | `/conversations` | Create a thread (`title` optional) | `201` |
| GET | `/conversations/{id}` | One thread, with `active_run_id` | `200` |
| PATCH | `/conversations/{id}` | Rename | `200` |
| DELETE | `/conversations/{id}` | Soft delete | `204` |
| GET | `/conversations/{id}/messages?before=&limit=` | Page of turns, oldest-first within the page | `200` |

Paged responses share one envelope:

```json
{
  "items": [ ... ],
  "next_cursor": "MjAyNi0wOS0wNlQxNjoyOTozMS4uLg",
  "has_more": true
}
```

`next_cursor` is `null` exactly when there is nothing more to read, so a client
loops until it is null rather than comparing counts to a page size. Cursors are
opaque: pass back what the server returned and never construct one. Conversations
page forward from the newest (`cursor`); messages page *backwards* into history
(`before`), because a thread is read from the bottom.

`limit` is bounded to 1–100 and defaults to 20.

`GET /conversations/{id}` also returns `active_run_id`: the id of the run
currently queued or running in the thread, or `null`. A page loaded mid-answer
has no stream; this is how it finds the run to follow through
`GET /runs/{run_id}`.

Only `user` and `assistant` turns are serialized. `system` and `tool` rows exist
in the same table and are never exposed — they would leak prompt text and raw
provider payloads.

A message carries its sources and its verification state:

```json
{
  "id": "...", "conversation_id": "...", "role": "assistant",
  "content": "Rates rose to 5% in March [S1].",
  "status": "complete", "sequence_number": 2, "created_at": "...",
  "degraded": false, "notice": null,
  "citations": [
    {
      "ordinal": 1, "source_id": "S1",
      "url": "https://news.example/rates", "display_url": "news.example/rates",
      "title": "...", "publisher": null, "snippet": "...",
      "published_at": null, "retrieved_at": "..."
    }
  ]
}
```

`[S1]` markers in `content` refer to `citations[].source_id`; `ordinal` numbers
the rendered source list in first-use order. `degraded` is true when a searched
answer could not be tied to a source or search was unavailable; `notice` is one
sentence explaining it, ready to show.

### Runs

A run is one execution of the agent answering one user message.

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| POST | `/conversations/{id}/runs` | Send a message (or retry the latest) and stream the answer | `200` `text/event-stream` |
| GET | `/runs/{run_id}` | Current state of a run — the recovery path | `200` |
| POST | `/runs/{run_id}/cancel` | Stop a run; idempotent | `200` |

**Starting a run.** The body carries exactly one of:

```json
{ "content": "What changed in the latest release?" }
{ "retry_message_id": "<id of the latest user message>" }
```

A retry answers the stored question again without creating a second copy of it;
only the conversation's latest message can be retried (`409 retry_not_allowed`
otherwise). `content` is trimmed, line endings are normalized, and it is limited
to `MAX_USER_MESSAGE_CHARS` (16,000; `422 message_too_long`).

The `Idempotency-Key` header is **required**: printable ASCII, 1–255 characters,
unique per logical request, and reused when retrying the same request.

- Same key, same request, run finished → `200` with header
  `Idempotent-Replayed: true` and a two-event stream: `run.started`
  (`"replayed": true`) and the original terminal event. Nothing is created and
  the model is not called.
- Same key, same request, run still in progress → `409 run_in_progress` with
  `details.run_id`; poll `GET /runs/{run_id}`.
- Same key, different request → `422 idempotency_key_reused`.

Keys are scoped per user. A conversation answers one message at a time: a
submission while another run is active returns `409 run_in_progress` with the
active `details.run_id`, and leaves nothing behind. The run lock usually refuses
it before any database work; `details.run_id` can then name a run whose
acceptance has not committed yet, so a poll of it may briefly return `404`. If
the lock backend (Redis) is unreachable, run creation returns
`503 service_unavailable`; retry shortly. Runs are rate-limited per
user (`RATE_LIMIT_RUNS` per `RATE_LIMIT_RUNS_WINDOW_SECONDS`, default 20/min).

Validation, ownership, idempotency, and conflict failures are ordinary JSON
error responses. Once the `200` stream starts, every outcome — including
failure — is reported *in* the stream.

### SSE protocol

`Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`. Each event is one frame:

```
id: 3
event: search.started
data: {"sequence":3,"run_id":"...","type":"search.started","query":"..."}

```

`data` is always one line of JSON repeating `sequence` and `type`, so a
`fetch`-based reader needs nothing but the `data` lines. `sequence` starts at 1
and increases by exactly one. Lines beginning with `:` are heartbeats (every
`SSE_HEARTBEAT_SECONDS`, default 15) and carry nothing.

| `type` | Fields | Notes |
| --- | --- | --- |
| `run.started` | `conversation_id`, `user_message`, `replayed` | Always first. `user_message` has the server id to reconcile an optimistic bubble. |
| `agent.status` | `status`: `thinking` \| `searching` \| `writing` | Coarse phase only; never reasoning. |
| `search.started` | `query` | The validated query. |
| `search.completed` | `query`, `sources[]`, `failed` | Source metadata only — no snippet, no page text. |
| `citation` | `citation` | Sent when a source is first cited, **before** the delta containing its marker. |
| `message.delta` | `text` | Answer text. Invalid citation markers are removed before it is sent. |
| `message.completed` | `message`, `run` | **Terminal.** `message` is canonical: replace the streamed text with `message.content`. |
| `run.failed` | `error` {`code`, `message`, `retryable`} | **Terminal.** |
| `run.cancelled` | `reason` | **Terminal.** `user_cancelled`, `client_disconnected`, or `conversation_deleted`. |

Exactly one terminal event ends every stream. Failure codes: `llm_unavailable`,
`run_timeout`, `run_interrupted` (server shutdown or a crashed process),
`internal_error`. A searched answer that could not be verified is not a
failure: it completes with `message.degraded: true`.

**Why replace streamed text on completion?** Deltas pass through the same
citation validation as the saved answer, so they agree on which sources are
cited, but the saved answer additionally has whitespace tidied where a marker
was removed. The canonical message is the one that reloads.

**Disconnects and recovery.** When the connection closes, the run keeps going
for `RUN_ABANDON_GRACE_SECONDS` (default 10) while it waits for someone to come
back. Each `GET /runs/{run_id}` from the owner renews that lease, so a reloaded
page that polls keeps its answer. A run nobody polls within the grace period is
cancelled (`client_disconnected`), which stops the model call rather than paying
for an answer nobody receives. An answer the model had already finished is
saved regardless. `GET /runs/{run_id}` returns the run with its
`assistant_message` embedded once complete; the message history shows the same.
Resumable streams (`Last-Event-ID`) are not supported in v1.

**Cancelling.** `POST /runs/{run_id}/cancel` stops the run and returns it; a
run that already finished is returned unchanged. A stopped response is not
saved — only the question remains, ready for `retry_message_id`.

`GET /runs/{run_id}` returns:

```json
{
  "id": "...", "conversation_id": "...",
  "user_message_id": "...", "assistant_message_id": "...",
  "status": "completed",
  "error": null, "cancel_reason": null,
  "search_count": 1,
  "usage": { "prompt_tokens": 812, "completion_tokens": 164 },
  "created_at": "...", "started_at": "...", "completed_at": "...",
  "assistant_message": { "...": "a message, as above" }
}
```

`status` is `queued`, `running`, `completed`, `failed`, or `cancelled`. `error`
is set only when `failed`; `cancel_reason` only when `cancelled`.

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
| `rate_limited` | 429 | Too many attempts or runs; see `Retry-After` and `details.retry_after_seconds` |
| `conversation_not_found` | 404 | Unknown id, or one owned by another user — deliberately the same |
| `run_not_found` | 404 | Unknown run, another user's run, or one in a deleted conversation |
| `message_not_found` | 404 | A retry named a message that is not a user message in this conversation |
| `run_in_progress` | 409 | The conversation is already answering; `details.run_id` names the run |
| `retry_not_allowed` | 409 | Only the latest message can be retried |
| `idempotency_key_reused` | 422 | The `Idempotency-Key` was used for a different request |
| `message_empty` | 422 | The message is blank after trimming |
| `message_too_long` | 422 | Over `MAX_USER_MESSAGE_CHARS`; `details` has the limit and length |
| `invalid_cursor` | 400 | Pagination cursor was not produced by this API |
| `service_unavailable` | 503 | A required dependency (Redis, for run creation) is down; retry shortly |
| `internal_error` | 500 | Unexpected failure; quote `request_id` |
