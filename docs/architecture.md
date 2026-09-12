# Architecture

Status: reflects Phase 5 (agent core). Updated as each phase lands — see
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
                ├── LLM provider  behind app/providers/llm.py
                └── Search        behind app/providers/search.py
```

## Backend layering

Each layer may call only the one below it.

| Layer | Location | Responsibility |
| --- | --- | --- |
| Routes | `app/api/v1/` | Validate HTTP input, call a service, map the result. No SQL, no business rules. |
| Services | `app/services/` | Orchestration and transactions. |
| Repositories | `app/repositories/` | All SQL. Every query is constrained by `user_id`. |
| Models | `app/models/` | SQLAlchemy ORM, mapped to Alembic migrations. |
| Agent | `app/agent/` | LangGraph state, nodes, routing, prompts, citation validation, checkpointer. |
| Providers | `app/providers/` | Narrow interfaces over the LLM and search vendors. No vendor type escapes. |

`app/core/` holds cross-cutting concerns: typed settings, security primitives,
logging, telemetry, rate limiting.

## Lifecycle

Shared clients are expensive and pooled, so they are built once in the FastAPI
lifespan (`app/main.py`) and closed on shutdown. `Database` (`app/db/session.py`)
owns the async engine; request handlers receive a session through the
`SessionDep` dependency and never construct an engine themselves.

The same lifespan builds the LLM and search providers, resolves `AgentLimits`
from settings, opens the LangGraph checkpointer, and compiles the agent graph
once. A graph compiled per request would rebuild its node table on every
message; a provider client per request would open a new connection pool.

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
non-https or wildcard `FRONTEND_ORIGIN`, a missing `LLM_API_KEY` or
`SEARCH_API_KEY`, and a synchronous database driver.

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

## Agent core

The agent is built and tested but not yet reachable over HTTP: Phase 6 adds the
run endpoint and SSE stream that call it. Design decisions are recorded in
[ADR-0007](adr/0007-agent-core.md).

```
START ─▶ decide_search ─┬─▶ web_search ─┬─▶ synthesize ─┐
                        │               │               ├─▶ finalize ─▶ END
                        └───────────────┴─▶ answer_direct ┘
                                (no sources)
```

The graph has no back edge, so every run reaches `finalize` in at most four
steps. The number of searches is bounded by a counter inside `web_search`, not by
repeating a node.

| Node | Responsibility |
| --- | --- |
| `decide_search` | Deterministic heuristics; a structured-output model call only when they abstain. Records a one-line rationale for logs. |
| `web_search` | Validates queries, runs at most `MAX_SEARCHES_PER_RUN` with a per-search timeout and one retry for transient failures, canonicalizes and deduplicates URLs, labels sources `S1…Sn`. |
| `synthesize` | Answers from sources under the untrusted-content prompt, then validates every citation marker against the retrieved sources. |
| `answer_direct` | Answers without sources; strips any citation marker the model writes. |
| `finalize` | Appends the answer to the checkpointed transcript and logs the outcome. Runs on every path. |

### State and context

`AgentState` (`app/agent/state.py`) is checkpointed after every step, so it holds
only JSON-native data: messages, identifiers, queries, sources, citations,
counters, the answer, and terminal error metadata. `AgentContext`
(`app/agent/context.py`) carries the user and run identity, the provider clients,
and the limits; it is passed to `ainvoke(context=…)` and never written to a
checkpoint.

`run_agent` (`app/agent/graph.py`) is the only entry point. It uses the
conversation id as the LangGraph `thread_id`, applies the total-run deadline, and
returns an `AgentResult` — the stable shape callers read instead of raw state.

### Failure behaviour

| Situation | Outcome |
| --- | --- |
| Decision model call fails | Answer directly, no search. |
| Search times out or returns 429/5xx | One retry after 0.5 s. |
| Every search fails, or a permanent error (bad key, quota) | Direct answer, `degraded`, with a user-facing notice. Not retried. |
| Search succeeds with zero usable results | Direct answer, no citations. |
| Model cites a label that was never retrieved | Marker stripped from the answer and logged. |
| Searched answer with no valid citation | `degraded`, with a notice that sources could not be verified. |
| Model unavailable | Terminal `llm_unavailable`; `retryable` only for transient errors. |
| Run exceeds `RUN_TIMEOUT_SECONDS` | Terminal `run_timeout`, retryable. |

### Checkpoints versus messages

LangGraph's checkpoint tables are created and migrated by the checkpointer
itself, not by Alembic, and no repository reads them. They are the graph's
recovery log; `messages` remains the application's history. The checkpointer
uses its own psycopg pool, separate from the asyncpg engine.

### Testing

Every agent test drives the real compiled graph with deterministic fakes
(`tests/fakes.py`), so CI needs no provider key. `tests/unit/agent/` covers
routing, budgets, retries, outages, empty results, invented citations, the run
deadline, and prompt injection; `tests/integration/test_checkpointer.py` proves
thread continuity, thread isolation, and that no client or credential reaches a
checkpoint.
