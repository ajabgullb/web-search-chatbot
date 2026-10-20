# Architecture

Status: reflects the complete v1 (all nine phases). Updated as it changes — see
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
                ├── PostgreSQL   application data + LangGraph checkpoints (pgvector image)
                ├── Redis        run locks, rate limits, cancel signals
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
structured logging, Prometheus metrics (`telemetry.py`), Redis coordination
(`redis.py`, `run_lock.py`, `run_signals.py`), and rate limiting. Optional
LangSmith tracing lives with the agent (`app/agent/tracing.py`) because it
records graph and prompt versions.

## Lifecycle

Shared clients are expensive and pooled, so they are built once in the FastAPI
lifespan (`app/main.py`) and closed on shutdown. `Database` (`app/db/session.py`)
owns the async engine; request handlers receive a session through the
`SessionDep` dependency and never construct an engine themselves.

The same lifespan builds the LLM and search providers, resolves `AgentLimits`
from settings, opens the LangGraph checkpointer, and compiles the agent graph
once. A graph compiled per request would rebuild its node table on every
message; a provider client per request would open a new connection pool.

It also builds the pieces behind PDF documents (ADR-0015): the embedding
provider (`app/providers/embeddings.py`), the PDF parser (`app/services/pdf.py`),
which starts a forkserver and parses and chunks each upload in a child
process of its own, and the ingestion manager (`app/services/ingestion.py`).
CPU-heavy work stays off the event loop: hashing an upload runs in a thread,
and embedding vectors are formatted as SQL text in a thread before they are
written (`docs/load-testing.md` has the measurements). An upload stores
extracted text as chunks and returns; the ingestion manager is a background
task, one per process, that writes the embeddings. Replicas share that work
through the database: a document is claimed with `FOR UPDATE SKIP LOCKED` and
held by a time lease, so a process that dies mid-document leaves it to the
next sweep, which resumes from the first chunk without a vector. On shutdown
the run manager drains first, then ingestion stops, then the providers and the
database close.

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
- `GET /api/v1/health/ready` returns `503` with a `checks` map when the
  process should not receive traffic. The map covers `database`, `redis` (when
  configured), and `accepting_work`, which goes false once shutdown starts
  draining.

## Database and migrations

PostgreSQL is the source of truth. Schema changes always ship an Alembic
migration with a working downgrade; `create_all` is never used outside isolated
tests. `alembic/env.py` takes its URL from `Settings`, so migrations cannot
target a different database than the API.

In development the single API container runs `alembic upgrade head` before
starting. Production runs migrations as an explicit release job so replicas do
not race (Phase 8).

PostgreSQL runs on the `pgvector/pgvector` image, which adds the `vector`
column type for document-chunk embeddings ([ADR-0015](adr/0015-chat-scoped-pdf-retrieval.md)).
The `documents` and `document_chunks` tables back the upload API and
document retrieval in answers (`app/services/retrieval.py`, selection rules in
`app/agent/tools/documents.py`); citations can point at a chunk and page. An append-only
`embedding_usage` table records tokens spent per user for the daily quota. The
image is Debian-based where the previous one was Alpine, so the migration
refuses a data volume carried across from the old image, whose text indexes
would be sorted under a different C library (`app/db/migrations_helpers.py`;
the safe move is in `docs/deployment.md` §5).

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
page can return the user there. `/reset-password` and `/verify-email` are
deliberately public: an emailed link must work whatever session the browser
holds. `/verify-email` re-reads the account through `refreshUser()` once both
the confirmation and the session bootstrap have finished, in either order, so
the chat's "confirm your email" banner (`VerifyEmailBanner`) disappears without
a reload.

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
It greets the user by the first word of their display name, with a line that
changes on each visit and never repeats the previous one.

On `md` and up the sidebar can be collapsed; the choice is a layout preference,
so it is remembered in `localStorage` (never anything auth-related). Below `md`
it is the off-canvas panel, independent of that setting.

TanStack Query retries are narrow: a 4xx is the server saying the request itself
is wrong, and repeating it only delays the error the user needs to see. Transient
failures still get one more attempt.

### Streaming frontend

Decisions are in [ADR-0009](adr/0009-streaming-frontend.md).

| Piece | Responsibility |
| --- | --- |
| `src/api/stream.ts` | Reads the SSE body with `fetch` (EventSource cannot POST or send a bearer token). A spec-following parser: `TextDecoder` in streaming mode keeps multi-byte characters intact across chunks; a line buffer handles events, lines, and CRLF split anywhere. |
| `src/api/client.ts` `openStream` | The same single-flight refresh-once rule as every request; replaying after a 401 is safe because it carries the same `Idempotency-Key`. |
| `features/chat/runStore.ts` | One live run per conversation, in a plain class outside React so an answer keeps streaming while the user switches threads. The only place stream events change state. |
| `features/chat/cache.ts` | Merges server-confirmed messages into the TanStack Query history, by id and sequence number. |
| `LiveTurn`, `MessageList`, `SourceList`, `Markdown` | The optimistic question, the streaming answer, inline citation buttons, and source cards. |

**Reconciliation.** The question is shown the moment it is sent. `run.started`
puts the stored question (with its server id) into the cache and drops the
optimistic copy; `message.completed` puts the canonical answer into the cache
*and* clears the streamed text in the same synchronous update, so the thread
never shows the answer twice or not at all.

**Stop** calls `POST /runs/{id}/cancel` — the server stops the model — and the
stream then ends with `run.cancelled`. Before the run has an id, stopping
aborts the request instead. **Retry** after a failed or stopped answer sends
`retry_message_id` under a new key; retrying a send that never arrived repeats
the same request under the *same* key, so the server replays it if the first
attempt did land.

**Recovery.** A dropped stream, or a page loaded while
`GET /conversations/{id}` reports an `active_run_id`, switches the run to
polling `GET /runs/{id}` — which also renews its lease — until it ends. A last
question with no answer and no active run offers a retry.

**Scrolling** follows the answer only while the reader is within 120 px of the
bottom. "Following" is decided by scroll events — which only the reader (or our
own scroll-to-bottom) produce — never by measuring after a render, so a large
chunk of text cannot flip it. A `ResizeObserver` catches growth that happens in
deferred renders and when the lazy Markdown chunk replaces its plain-text
fallback.

**Accessibility.** One `aria-live="polite"` region announces each phase change
(thinking, searching, writing, complete, stopped, failed) — never the tokens.
The streaming answer is `aria-busy`. Citation buttons are named "Source N:
title" and move focus to their source card. The composer stays editable while
an answer streams, so a keyboard user's focus is never thrown out; only
sending is blocked, and the send button becomes Stop.

## Agent core

The agent is reached through runs (next section). Design decisions are recorded
in [ADR-0007](adr/0007-agent-core.md).

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

`stream_agent` (`app/agent/graph.py`) is the entry point. It takes the
conversation history, uses the conversation id as the LangGraph `thread_id`,
and yields progress events (`app/agent/events.py`) followed by exactly one
`AgentResult` — the stable shape callers read instead of raw state. Nodes emit
events through LangGraph's custom stream channel; answers are always streamed
from the model, whether or not anyone is listening, so there is one code path.
`run_agent` drains the same stream under the run deadline; tests use it.

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
| Model rejects the key (401/403) | Terminal `llm_auth_failed`, not retryable, logged as a warning so a bad or stale key is not mistaken for an outage. |
| Run exceeds `RUN_TIMEOUT_SECONDS` | Terminal `run_timeout`, retryable. |

### Checkpoints versus messages

The `messages` table is the one canonical conversation history. Each run loads
the latest `MAX_CONTEXT_MESSAGES` turns from it (every user turn; assistant
turns only when complete, with old citation markers stripped because their
labels belonged to another run's sources) and hands them to the graph, which
*replaces* the checkpoint's transcript with them rather than appending. So a
failed run, a cancelled run, or a retry can never leave the checkpoint holding
a turn the product does not show — the problem an append-only transcript had.

The checkpoint remains the graph's per-step record for the thread. Its tables
are created and migrated by the checkpointer itself, not by Alembic, and no
repository reads them; `alembic/env.py` excludes them from autogenerate, which
would otherwise propose dropping them. The checkpointer uses its own psycopg
pool, separate from the asyncpg engine.

### Testing

Every agent test drives the real compiled graph with deterministic fakes
(`tests/fakes.py`), so CI needs no provider key. `tests/unit/agent/` covers
routing, budgets, retries, outages, empty results, invented citations, the run
deadline, prompt injection, the event sequence, and streaming citation
validation under arbitrary chunking; `tests/integration/test_checkpointer.py`
proves the checkpoint mirrors the supplied history (no duplicate turn after a
retry), thread isolation, and that no client or credential reaches a
checkpoint.

## Runs and streaming

A run is one execution of the agent for one user message. Design decisions are
in [ADR-0008](adr/0008-runs-and-streaming.md); the wire protocol is in
[api.md](api.md#sse-protocol).

```
POST /conversations/{id}/runs
  │  RunService.create — one short transaction:
  │    ownership · idempotency lookup · stale-run sweep ·
  │    INSERT user message + run (queued) · COMMIT
  │    (unique-index violation → replay, or 409)
  ▼
RunManager.start ──▶ asyncio task ─────────────────────────────┐
  │                   mark running + load history (short txn)  │
  │                   stream_agent under the run deadline      │
  │                   internal events ─▶ numbered SSE events ──┼─▶ queue
  │                   persist outcome (shielded, one txn)      │
  ▼                                                            │
EventStreamResponse ◀── live_events: queue → frames, heartbeats ┘
      └─ generator torn down early (client gone) ─▶ cancel the task
```

**Two lifetimes, deliberately separate.** The HTTP response lives as long as the
connection; the run lives as long as the work. The run executes in its own task
(`app/services/run_executor.py`) and publishes events to a per-run queue that the
SSE generator (`app/api/sse.py`) relays. That split gives both halves of the
contract: a run nobody is watching is cancelled, which interrupts the model
stream; and an answer the model has finished is persisted even if the
connection is gone, because the final write is shielded from cancellation and
does not depend on the client.

**Watching is a lease, not a connection.** A page reload drops the stream and
reconnects a second later by polling `GET /runs/{id}`. So a dropped stream
does not cancel the run at once: it starts a `RUN_ABANDON_GRACE_SECONDS` lease
(default 10 s), each poll from the owner renews it, and only a run nobody
renews is cancelled as `client_disconnected`. See
[ADR-0009](adr/0009-streaming-frontend.md).

**The database is the coordinator.** At most one queued/running run per
conversation is a partial unique index; idempotency is a unique
`(user_id, idempotency_key)`; every state change is a conditional `UPDATE …
WHERE status IN ('queued','running')`, so a completing executor, a cancel
request, and the stale sweep can race and exactly one wins. None of this needs
Redis or a second replica to be correct.

**A run lock sits in front of it.** Before accepting a run, the service takes
a per-conversation lease (`app/core/run_lock.py`) in Redis, or in process
memory when `REDIS_URL` is empty. The lease is named by the run's id and
expires after the run deadline plus the stale grace period. It turns a
conflicting submission away before any database work, and a cancel on any
replica frees it at once. The executor releases it after recording the
outcome. If the lock is lost, the index still refuses the second run. A Redis
outage fails run creation with `503`. See
[ADR-0010](adr/0010-redis-run-lock.md).

**Short transactions.** Accepting a run, marking it started, and recording its
outcome are three separate short transactions. No transaction is open while
the model or search provider is working.

**Sequence numbers** are assigned by the run's task, the only producer on its
queue, so they are strictly increasing by construction. Internal events become
wire events in exactly one function (`RunManager._to_public`), so nothing
reaches a client that the mapping does not explicitly carry.

**Cancellation** is cooperative and has three paths. In-process, the task is
cancelled, stopping the model call at its next await, and it records the
outcome itself. If the run is not local, the cancel is broadcast on the Redis
channel `run-cancel` (`app/core/run_signals.py`), and the replica executing it
cancels its task the same way. Conversation deletes are broadcast too. Either
way, the cancel endpoint also marks the run cancelled in the database,
conditioned on it still being active. That is the durable record: pub/sub is
best effort, and a replica that missed the signal (or a dead process) finds the
row no longer active and discards its result. A run already stopping ignores
further cancels, so a second cancel cannot interrupt the shielded write that
records the first.

**Crash recovery.** A process that dies mid-run leaves a `running` row. No
healthy run outlives `RUN_TIMEOUT_SECONDS`, so a run older than that plus
`RUN_STALE_GRACE_SECONDS` is presumed dead and failed as `run_interrupted`
(retryable) the next time its conversation is used or the run is polled.

**Graceful shutdown.** On SIGTERM, uvicorn closes its listener and lets open
responses finish, run streams included, for up to `--timeout-graceful-shutdown`
(100 s in the production image, longer than the 90 s run deadline). The
lifespan then runs `RunManager.shutdown`. It stops accepting runs (new
submissions get `503` with `Retry-After`, and readiness reports
`accepting_work: false`). It waits up to `SHUTDOWN_DRAIN_SECONDS` for runs with
no open stream, such as a reloaded page that is polling, and only then cancels
what is left, recording it as `run_interrupted` (retryable). The shutdown
releases run locks before it closes Redis and the database pool.
`tests/integration/test_graceful_shutdown.py` runs a real uvicorn server and
checks that a stream open at shutdown still delivers the whole answer.

| Outcome | Run status | Assistant message | Terminal event |
| --- | --- | --- | --- |
| Answer produced | `completed` | saved with citations | `message.completed` |
| Model or search budget failure | `failed` + code | none | `run.failed` |
| User pressed stop / tab closed / conversation deleted | `cancelled` + reason | none | `run.cancelled` |
| Server shutdown or crashed process | `failed`, `run_interrupted` | none | `run.failed` |

## Testing and evaluation

Each layer tests what the one below cannot. See the table in the README for
commands.

- **Unit and integration** (`backend/tests`, `frontend/src/**/*.test.*`).
  Every agent test drives the real compiled graph with scripted providers.
  Integration tests use a real PostgreSQL database (a `_test` sibling of the
  development one) and fakeredis. Concurrency, cancellation, and shutdown are
  tested with real tasks and, for SIGTERM, a real uvicorn server.
- **Contract** (`backend/tests/contract`, `frontend/src/api/contract.test.ts`).
  The committed schema must equal the app's, and every operation must document
  the error envelope. The stream parser must know exactly the event types the
  schema publishes: TypeScript can check that each listed type exists, but not
  that none is missing, and a missing one would be dropped silently.
- **End to end** (`frontend/tests/e2e`). Playwright drives Chromium against
  the real API, started from `backend/tests/e2e_server.py` with scripted LLM
  and search providers on its own database, and a real Vite server. Everything
  but the two paid dependencies is the production code path.
- **Agent evals** (`backend/evals`). A versioned dataset covering every
  AGENTS.md §13 category. The `ci` subset is deterministic and gated at 100%.
  The `live` subset uses the configured model and an LLM judge, and is run by
  hand. See [ADR-0012](adr/0012-testing-and-evaluation.md).

