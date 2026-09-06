# Architecture

Status: reflects Phase 3 (authentication frontend). Updated as each phase lands — see
[`PLAN.md`](../PLAN.md) for what is coming and [`AGENTS.md`](../AGENTS.md) for
the contract this implements.

## Shape

A monorepo with two deployables and one database.

```
browser ──▶ frontend (Vite/React, nginx in production)
                │  /api/* proxied to the API (same origin)
                ▼
           backend (FastAPI, async)
                │
                ├── PostgreSQL   application data + LangGraph checkpoints
                ├── Redis        rate limits, run locks, cancel flags (Phase 8)
                ├── LLM provider  behind app/providers/llm.py    (Phase 5)
                └── Search        behind app/providers/search.py (Phase 5)
```

## Backend layering

Each layer may call only the one below it.

| Layer | Location | Responsibility |
| --- | --- | --- |
| Routes | `app/api/v1/` | Validate HTTP input, call a service, map the result. No SQL, no business rules. |
| Services | `app/services/` | Orchestration and transactions. |
| Repositories | `app/repositories/` | All SQL. Every query is constrained by `user_id`. |
| Models | `app/models/` | SQLAlchemy ORM, mapped to Alembic migrations. |
| Agent | `app/agent/` | LangGraph state, nodes, routing, prompts, events. |
| Providers | `app/providers/` | Narrow interfaces over the LLM and search vendors. No vendor type escapes. |

`app/core/` holds cross-cutting concerns: typed settings, security primitives,
logging, telemetry, rate limiting.

## Lifecycle

Shared clients are expensive and pooled, so they are built once in the FastAPI
lifespan (`app/main.py`) and closed on shutdown. `Database` (`app/db/session.py`)
owns the async engine; request handlers receive a session through the
`SessionDep` dependency and never construct an engine themselves.

## Configuration

Every configurable value is declared on `Settings` (`app/core/config.py`) and
documented in `.env.example`. No other module reads `os.environ`.

Dependencies resolve settings from `request.app.state.settings`
(`get_app_settings`), **not** from the cached `get_settings()` singleton. The
singleton is used once, to construct the app. Reading it inside a dependency
would let an app built with one configuration behave according to another —
which is exactly the bug the Phase 2 cookie tests caught.

The single `.env` lives at the repository root so Compose and the backend see
identical values; the backend resolves it by path rather than by working
directory. Production startup fails closed on a placeholder `JWT_SECRET`, a
non-https or wildcard `FRONTEND_ORIGIN`, and a synchronous database driver.

`DATABASE_URL` must use the `postgresql+asyncpg` driver — a synchronous driver
is rejected at startup instead of silently blocking the event loop.

## Request pipeline

Middleware, outermost first:

1. `RequestIdMiddleware` — adopts an inbound `X-Request-ID` or mints one, puts
   it in a `ContextVar`, and echoes it on the response. Every log line and
   error body carries it.
2. `CORSMiddleware` — explicit origins, credentials enabled, never a wildcard.
3. `SecurityHeadersMiddleware` — `nosniff`, frame denial, referrer policy, CSP,
   `no-store`, and HSTS in production.

Exception handlers (`app/api/errors.py`) convert every failure — an `AppError`
subclass, a validation error, a framework `HTTPException`, or an unhandled
exception — into the same envelope. Handlers raise domain errors; they never
build error responses themselves.

## Health

- `GET /api/v1/health/live` — process liveness. Deliberately independent of
  every external dependency, so an LLM or database outage cannot trigger a
  restart loop.
- `GET /api/v1/health/ready` — returns `503` with a per-dependency `checks` map
  when a required dependency is unreachable.

## Database and migrations

PostgreSQL is the source of truth. Schema changes always ship an Alembic
migration with a working downgrade; `create_all` is never used outside isolated
tests. `alembic/env.py` takes its URL from `Settings`, so migrations cannot
target a different database than the API.

In development the single API container runs `alembic upgrade head` before
starting. Production runs migrations as an explicit release job so replicas do
not race (Phase 8).

## Frontend

Vite + React 19 + TypeScript strict. Routing via React Router, server state via
TanStack Query, styling via Tailwind CSS v4 tokens with shadcn/ui conventions
(`src/styles/globals.css`). The dev server proxies `/api` to the backend so the
browser sees a single origin — the same shape as production behind a proxy,
which keeps cookie and CORS behaviour honest in development.

### The API client

`src/api/generated/schema.d.ts` is generated from `openapi.json` and never
hand-edited; `src/api/client.ts` is hand-written because it holds the session.
See [ADR-0004](adr/0004-generated-api-client.md).

Three invariants live in `client.ts`:

1. The access token is a module variable — never `localStorage`, never
   `sessionStorage`. ESLint fails the build on either, in both the bare-global
   and `window.` spellings.
2. Concurrent 401s share **one** refresh. Rotation is single-use, so a second
   concurrent refresh would present a retired token and the server would revoke
   the whole family.
3. A request is replayed at most once, and only when refreshing actually
   returned a token. There is no code path that can loop.

### Session lifecycle

A reload loses the in-memory token but keeps the HttpOnly refresh cookie, so
`AuthProvider` makes exactly one refresh attempt on mount and then settles into
`authenticated` or `anonymous`. Failure is not an error — it is the normal
state of a visitor — so it never redirects. Route guards wait out the
`bootstrapping` state before deciding, which is what stops a signed-in user
being bounced to `/login` on every reload.

`RequireAuth` stores the attempted location in navigation state so the login
page can return the user there. `/reset-password` is deliberately public: a
stale session in another tab must not block a reset from an emailed link.

## Conversation domain

Three layers, no shortcuts between them:

```
routes/conversations.py   parse, delegate, serialize — no ownership logic
services/conversations.py orchestration, titles, cursor encode/decode, commits
repositories/            every query constrained by user_id, all the SQL
```

Ownership is not a check a handler performs; it is a property of every query the
repository issues. `ConversationRepository.get(user_id=…, conversation_id=…)`
has no overload that omits the owner, so there is no code path that could forget.
`MessageRepository` joins `conversations` and constrains `user_id` there too,
even though the service has already resolved the thread — a future caller that
passes an unverified id still cannot reach another user's messages.

Titles are derived locally from the first user message: whitespace collapsed,
cut on a word boundary near 80 characters. No model call, because titling must
never sit on the critical path of the first answer (`AGENTS.md` §10). A title
the user chose is never overwritten.

Pagination and soft deletion are recorded in
[ADR-0005](adr/0005-cursor-pagination-and-soft-deletion.md); Markdown rendering
in [ADR-0006](adr/0006-markdown-rendering.md).

### Chat frontend

`ChatLayout` owns the sidebar so switching threads does not unmount and refetch
the history list. `/chat` is an empty state that deliberately does **not**
auto-create a conversation — a user who lands there, reloads, or signs back in
would otherwise leave a trail of empty threads.

TanStack Query retries are narrow: a 4xx is the server saying the request itself
is wrong, and repeating it only delays the error the user needs to see. Transient
failures still get one more attempt.
