# Architecture

Status: reflects Phase 2 (authentication backend). Updated as each phase lands — see
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
