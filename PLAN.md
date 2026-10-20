# PLAN.md — Phased implementation roadmap

This document expands [`AGENTS.md`](./AGENTS.md) §15 into nine sequential, independently shippable phases. `AGENTS.md` remains the authoritative contract; this file is the execution order.

**How to use it**

- Phases are **sequential**. Do not start phase N+1 while phase N's definition of done is unmet.
- The repository must be **runnable after every phase** — `docker compose up --build` works and the test suite passes.
- Build **vertical slices**. Do not scaffold files ahead of the phase that uses them.
- Every schema change ships an **Alembic migration with a downgrade path**.
- Provider calls are **faked in CI**. No test may require a paid API.
- Ownership is enforced **at the SQL boundary**; a guessed UUID returns `404`, never `403`.
- Whenever the public schema changes, **regenerate and commit** OpenAPI and the TypeScript client in the same change.

**Confirmed technical decisions**

- LLM provider: `openai_compatible` via `langchain-openai`, base URL configurable (OpenAI, OpenRouter, Groq, Ollama). One implementation behind the `LLMProvider` interface, per `AGENTS.md` §11.
- Search provider: Tavily behind the `SearchProvider` interface.
- Verified local toolchain: uv 0.10.6, Python 3.12.3, Node 24.14.0, npm 11.18.0, Docker 29.7.2 + Compose v5.5.0, GNU Make 4.3, psql 16.15.

**Mapping to `AGENTS.md` §15**

| §15 phase | Phases here |
| --- | --- |
| 1 Foundation | 1 |
| 2 Authentication | 2, 3 |
| 3 Conversation domain | 4 |
| 4 Agent core | 5 |
| 5 Streaming | 6, 7 |
| 6 Production hardening | 8, 9 |

---

## Phase 1 — Foundation

**Goal:** Stand up the monorepo, the database, and the verification loop that every later phase depends on.

**Features**

- Root scaffolding: `Makefile`, `.gitignore`, `.dockerignore`, `.env.example`, `README.md`, `compose.yaml` with `api`, `web`, `postgres`, and `mailpit` services.
- `Makefile` targets from §14: `dev`, `down`, `logs`, `migrate`, `migration name=…`, `test`, `test-backend`, `test-frontend`, `e2e`, `lint`, `format`, `typecheck`, `openapi`, `generate-client`.
- Backend project: `uv` with `pyproject.toml` and a committed `uv.lock`; Ruff format + lint; mypy in strict-enough mode.
- Typed settings in `app/core/config.py` using pydantic-settings, covering the full `.env.example` key list from §11.
- FastAPI app factory in `app/main.py` with a lifespan that constructs the async engine and shared clients once and closes them on shutdown.
- `app/db/session.py` (SQLAlchemy 2 async, bounded pool) and `app/db/base.py` with the UUID + UTC timestamp mixins.
- Alembic wired to the async URL, with an empty baseline revision. `create_all` is never used outside isolated tests.
- `GET /api/v1/health/live` and `GET /api/v1/health/ready` — readiness checks database connectivity, liveness depends on nothing external.
- Frontend project: Vite + React 19 + TypeScript strict, Tailwind CSS, shadcn/ui initialised, ESLint + Prettier, React Router shell with a placeholder landing route.
- `docs/` skeleton: `architecture.md`, `api.md`, `security.md`, `adr/0001-record-architecture-decisions.md`.
- GitHub Actions skeleton: install from lockfiles, lint, typecheck, build both apps, run migrations against a disposable PostgreSQL service.

**Definition of done**

- `docker compose up --build` from a clean clone serves the API and the Vite dev server.
- `/health/ready` reports database connectivity; `/health/live` stays green when external providers are down.
- `alembic upgrade head` succeeds on an empty database and `alembic downgrade base` returns it to empty.
- Ruff, mypy, ESLint, Prettier, and `tsc --noEmit` all pass.
- CI is green on the skeleton pipeline.

**Verify**

```bash
make dev
make migrate
make lint && make typecheck
curl -s localhost:8000/api/v1/health/ready
curl -s localhost:8000/api/v1/health/live
```

---

## Phase 2 — Authentication backend

**Goal:** Implement all of `AGENTS.md` §6 server-side, with no UI yet.

**Features**

- Migrations for `users`, `auth_sessions`, and `password_reset_tokens`, including the indexes for active sessions and normalized unique email.
- `app/core/security.py`: Argon2id password hashing, reasonable minimum length, long password-manager passwords allowed.
- Short-lived signed access JWT (default 15 min) with issuer/audience/expiry claims.
- Opaque rotating refresh token (default 30 days), stored only as a cryptographic hash, with `family_id`, `expires_at`, `revoked_at`, `replaced_by_id`, user agent, and optional IP fingerprint.
- Mandatory refresh rotation with **reuse detection that revokes the whole token family**.
- Endpoints: `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all`, `/auth/forgot-password`, `/auth/reset-password`, and `GET /users/me`.
- Refresh token delivered in a `Secure`, `HttpOnly`, `SameSite=Lax` cookie scoped to the refresh endpoint. The access token is returned in the response body only.
- Email behind an interface with a Mailpit implementation for development; password-reset responses never disclose whether an email exists.
- Single-use hashed reset tokens with expiry and `used_at`.
- Cross-cutting HTTP layer: the stable error envelope from §8, `X-Request-ID` propagation/creation, explicit CORS origins from config, `Origin` validation on cookie-authenticated state-changing endpoints, and the baseline security headers (`nosniff`, referrer policy, frame denial).

**Definition of done**

- Integration tests against real PostgreSQL prove refresh rotation, reuse detection revoking the family, logout, and logout-all.
- No password, token, cookie, or authorization value appears in any log line.
- The full forgot → email → reset flow works end to end through Mailpit, and responses for existing and non-existing emails are indistinguishable.
- Unauthenticated `GET /users/me` returns `401` inside the error envelope with a `request_id`.
- Rate limits guard login, register, and forgot-password.
- OpenAPI regenerated and committed.

**Verify**

```bash
make test-backend
uv run pytest tests/integration/test_auth_rotation.py -v
make openapi && git diff --stat
open http://localhost:8025   # Mailpit
```

---

## Phase 3 — Authentication frontend

**Goal:** Give users a real way in, with an auth bootstrap that cannot loop.

**Features**

- Public landing page at `/` explaining the product with sign-up / log-in actions.
- Routes `/login`, `/register`, `/forgot-password`, `/reset-password`, plus a catch-all 404 page.
- Generated API client committed under `src/api/generated/`, wrapped by a hand-written `src/api/client.ts` (never edit generated files by hand).
- Access token held **in memory only** — never `localStorage`, never `sessionStorage`.
- Auth bootstrap: exactly one refresh attempt on load, concurrent refresh calls deduplicated into a single in-flight request, redirect to `/login` on failure.
- Route guards that preserve and restore the intended destination after login.
- TanStack Query provider with sane defaults, plus form validation and inline error states.
- Light and dark themes; WCAG 2.1 AA baseline — semantic controls, keyboard navigation, visible focus, sufficient contrast, reduced-motion support.

**Definition of done**

- A grep of the frontend finds no auth token written to browser storage.
- Vitest proves the single-flight refresh: N simultaneous 401s produce exactly one refresh call, and a failed refresh cannot produce an infinite retry loop.
- Guarded routes redirect while unauthenticated and land on the originally requested route after login.
- ESLint and `tsc --noEmit` are clean.
- The committed client matches the current OpenAPI with no drift.

**Verify**

```bash
make generate-client && git diff --exit-code frontend/src/api/generated
make test-frontend
make typecheck
grep -rn "localStorage" frontend/src   # no auth tokens
```

---

## Phase 4 — Conversation domain

**Goal:** Ownership-safe conversation and message CRUD, with a usable chat shell on top of it.

**Features**

- Migrations for `conversations` (with `deleted_at` soft delete) and `messages` (role, normalized content, status, `sequence_number`, model name, token/latency metadata), including the unique `(conversation_id, sequence_number)` constraint and the listing/pagination indexes.
- Repositories where **every** query is constrained by `user_id` at the SQL boundary.
- Endpoints: `GET/POST /conversations`, `GET/PATCH/DELETE /conversations/{id}`, `GET /conversations/{id}/messages` — all cursor-paginated (`cursor`/`limit`, `before`/`limit`), never offset.
- Deterministic conversation title derived from the first user message; no LLM call on the critical path.
- Public message serialization exposes user and assistant roles only.
- Regenerated and committed API client.
- Chat shell UI: responsive sidebar with new chat, paginated history, rename and delete actions, account menu, sign out; routes `/chat` and `/chat/:conversationId`.
- Message list rendering **sanitized** Markdown with syntax-highlighted fenced code and a copy action.
- Composer: multiline input, Enter to send, Shift+Enter for a newline, visible character limit (16,000), accessible labels.

**Definition of done**

- Authorization tests prove a second user receives `404` — never `403` — for every conversation and message route, including guessed UUIDs.
- Cursor pagination stays stable when rows are inserted mid-listing.
- Soft-deleted conversations vanish from listings and return `404` on direct access.
- Reloading `/chat/:conversationId` restores the conversation and its messages.
- No ORM object is returned directly from any route; all responses are Pydantic models.
- Model output is never injected as unsanitized HTML.

**Verify**

```bash
make test
uv run pytest tests/integration/test_ownership.py -v
make generate-client && git diff --exit-code frontend/src/api/generated
# manual: reload /chat/:id, rename, delete, paginate the sidebar
```

---

## Phase 5 — Agent core

**Goal:** Build the LangGraph agent and prove it entirely through tests, before any HTTP surface exists.

**Features**

- `app/providers/llm.py` — `openai_compatible` implementation via `langchain-openai`, configurable base URL and output-token limits, behind a narrow interface.
- `app/providers/search.py` — Tavily implementation behind a narrow interface. No provider-specific object escapes either module.
- Deterministic fake LLM and fake search providers for tests and CI.
- Typed graph state in `app/agent/state.py`: messages with the appropriate reducer, run/conversation identifiers, search decision plus a short log-safe rationale, queries, normalized results, normalized citations, search iteration count, terminal error metadata.
- `app/agent/context.py` — authenticated identity, provider clients, limits, and request metadata passed via runtime context, never serialized into state.
- The explicit graph in `app/agent/graph.py`: `START -> decide_search -> [web_search -> synthesize | answer_direct] -> finalize -> END`.
- `decide_search`: deterministic heuristics first, structured model output as fallback. Searches for fresh, niche, externally verifiable, source-requesting, or uncertain questions; does not search greetings, rewrite requests, or questions answerable from the visible conversation.
- `web_search`: validated queries only, URL canonicalization and deduplication, result caps, per-search timeout, one bounded exponential-backoff retry for transient failures only — never for auth, validation, or quota failures.
- Result normalization contract: `source_id`, `url`, `title`, `snippet`, optional `publisher`, optional `published_at`, `retrieved_at`, plus a safe display URL. Only `http`/`https` permitted.
- `synthesize` / `answer_direct` / `finalize`, with post-generation citation validation that discards unknown IDs, persists sources in first-use order, and marks a searched answer carrying factual claims but zero valid citations as **degraded**.
- Versioned prompts in `app/agent/prompts.py` implementing the full §7 prompt contract, including the clause that retrieved web content is untrusted data and cannot override instructions, request secrets, or authorize tools. A non-sensitive prompt/version identifier is recorded on runs.
- LangGraph PostgreSQL checkpointer, owning its own tables separately from application message tables.
- Context trimming that preserves the latest messages and system constraints and never silently truncates the newest user message.
- Budgets with a hard loop counter and terminal route: max 3 searches per run, 5 results per search, 20 s per search, 90 s per run — all configurable.

**Definition of done**

- Graph transition tests cover both branches plus budget exhaustion, search outage, empty results, malformed citation IDs, and the total-run deadline.
- A prompt-injection fixture embedded in retrieved content changes no tool argument and leaks no system prompt.
- Search-decision tests cover the positive and negative cases named in §7.
- Checkpointer continuity is proven by resuming an existing thread and getting coherent state.
- CI runs the whole agent suite with fakes and touches no paid API.

**Verify**

```bash
uv run pytest tests/unit/agent -v
uv run pytest tests/integration/test_checkpointer.py -v
uv run pytest tests/unit/agent/test_prompt_injection.py -v
```

---

## Phase 6 — Runs, streaming, and cancellation (backend)

**Goal:** Expose the agent over the SSE contract with correct persistence, idempotency, concurrency, and cancellation.

**Features**

- Migrations for `runs` (status, error code, timings, provider/model, search count, usage metadata) and `citations` (stable ordinal, canonical URL, title, publisher, snippet, retrieved/published timestamps).
- `POST /conversations/{conversation_id}/runs` — accepts the user message and streams SSE: `Content-Type: text/event-stream`, proxy buffering disabled, heartbeat comments, monotonically increasing sequence numbers, JSON data.
- Event types: `run.started`, `agent.status` (`thinking`, `searching`, `writing`), `search.started` (sanitized query), `search.completed` (source metadata only, never page content), `message.delta` (text only), `citation`, `message.completed`, `run.failed` (stable error code + retryability), `run.cancelled`.
- `Idempotency-Key` on run creation — a repeated key for the same user/request creates no duplicate message or run and returns the original.
- Single-active-run-per-conversation coordination; conflicting submissions get `409`.
- `POST /runs/{run_id}/cancel` with cooperative cancellation: set the flag, cancel local tasks, check between graph and tool steps, persist `cancelled`.
- `GET /runs/{run_id}` as the recovery/polling path.
- Transactional commit of the assistant message, citations in first-use order, usage metadata, and run status — persisted even if the transport dies after model completion.
- Client-disconnect detection that stops work rather than burning budget.
- Database transactions stay short; none is held open across an LLM or search call.
- Regenerated and committed API client.

**Definition of done**

- SSE framing tests assert event ordering, sequence monotonicity, heartbeats, and terminal-event exclusivity.
- A killed client connection stops server-side work.
- A cancelled run persists as `cancelled` and the partial assistant message is handled per contract.
- A replayed `Idempotency-Key` returns the original run and creates nothing new.
- Two concurrent submissions to one conversation produce exactly one run and one `409`.
- No event ever contains hidden reasoning, raw provider payloads, prompts, or secrets.
- A stream interrupted after model completion still leaves the final message retrievable via `GET /runs/{id}` and the message history.

**Verify**

```bash
uv run pytest tests/integration/test_sse.py tests/integration/test_idempotency.py tests/integration/test_concurrency.py -v
curl -N -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)" \
  -X POST localhost:8000/api/v1/conversations/$CID/runs -d '{"content":"latest news on X"}'
make generate-client && git diff --exit-code frontend/src/api/generated
```

---

## Phase 7 — Streaming frontend

**Goal:** Render the stream incrementally and reconcile it with the canonical persisted response.

**Features**

- `src/api/stream.ts` — an SSE reader that survives chunk splits across event and UTF-8 boundaries, with typed event handling and clean teardown.
- Incremental assistant rendering into sanitized Markdown with syntax-highlighted code.
- Inline citation buttons tied to source cards; all outbound links use `rel="noopener noreferrer"` and are restricted to `http`/`https` after validation.
- Optimistic user-message display, reconciled against server IDs, with clear failure marking.
- Stop and retry controls; stop calls the cancel endpoint, not just the local UI.
- TanStack Query cache reconciliation between streamed text and the persisted canonical message on `message.completed`.
- Auto-scroll only when the user is already near the bottom; manual scrolling is never overridden.
- Composer disabled/limited while a run is active.
- Status announcements through an ARIA live region; loading, error, and degraded-answer states.
- Reload-mid-stream recovery via `GET /runs/{id}` plus message history.

**Definition of done**

- Vitest proves the parser handles payloads split mid-event and mid-multibyte-character.
- Reloading the page mid-stream recovers the run state and final message.
- Stop cancels server-side, verified by the run persisting as `cancelled`.
- Retry works after a failed or stopped response without duplicating the user message.
- No `dangerouslySetInnerHTML` receives unsanitized model output.
- Streamed content and the persisted message agree after completion — no duplicated or dropped text.

**Verify**

```bash
make test-frontend
make typecheck
# manual against fake providers: stream, stop, retry, reload mid-stream, scroll up during streaming
```

---

## Phase 8 — Production hardening

**Goal:** Make the system safe to run unattended and fail closed when misconfigured.

**Features**

- Redis for distributed rate limiting, cancellation flags, and run locks. The API fails clearly at startup if the production profile is configured without Redis; Redis stays optional in local development.
- Production config validation rejecting default or weak secrets, credentialed wildcard CORS, missing provider configuration, and development debug settings.
- Structured JSON logging with the §12 field set (timestamp, level, event, request ID, opaque user ID, conversation ID, run ID, duration, status, provider, model, search count, error code) and redaction of secrets, auth headers, cookies, prompts, message bodies, and page content.
- Prometheus metrics at `/metrics`, protected or isolated in production: request count/latency/error rate, active streams, run count/duration/outcome, time-to-first-token, token usage, search latency and failures, rate-limit hits, DB pool saturation, provider failures.
- Timeouts everywhere — request body limits, message length, output tokens, search results, per-tool timeouts, total-run deadline — plus bounded connection pools.
- Graceful shutdown: stop accepting new work, drain in-flight requests within a short window.
- Content Security Policy appropriate to the frontend, alongside the headers added in Phase 2; HSTS documented at the reverse proxy.
- Optional LangSmith tracing enabled purely by environment, with environment tags, graph/prompt version metadata, and privacy-safe inputs. The app works when tracing is disabled or unavailable.
- Multi-stage, non-root Dockerfiles with health checks and `.dockerignore`; no secrets baked into images.
- `compose.prod.yaml` running migrations as an explicit release/prestart job, not concurrently from every replica.

**Definition of done**

- Booting with `ENVIRONMENT=production` and a default `JWT_SECRET`, wildcard CORS, or missing provider config fails at startup with a clear, actionable message.
- `/metrics` exposes run duration, time-to-first-token, search latency/failures, rate-limit hits, and pool saturation.
- Rate limits return `429` with retry metadata; unavailable dependencies return `503`.
- SIGTERM drains in-flight streams instead of dropping them.
- Both images run as a non-root user, pass their health checks, and contain no secrets.
- A log scan over a full run finds no prompt bodies, page content, cookies, or tokens.

**Verify**

```bash
docker compose -f compose.prod.yaml up --build
uv run pytest tests/integration/test_prod_config.py -v
curl -s localhost:8000/metrics | head -40
docker compose logs api | grep -iE "password|bearer|cookie|api[_-]key"   # expect no matches
```

---

## Phase 9 — End-to-end, evaluations, and documentation

**Goal:** Prove the whole product works from a clean clone and lock it in with gates and docs.

**Features**

- Playwright suite: register and log in, create a conversation, receive a streamed cited answer from fake providers, reload history, cross-user ownership denial, stop a response, log out, and a mobile layout smoke test.
- `backend/evals/` — a small versioned dataset covering the nine §13 cases: no-search, freshness, multi-source, conflicting sources, prompt injection in retrieved content, search outage, empty results, malformed citation, and conversation follow-up.
- Eval metrics: search-decision accuracy, citation validity and precision, task-answer rubric score, unsupported-claim rate, latency, and approximate cost.
- A fast deterministic eval subset gated in CI; model-based evaluations documented as a separate manual run.
- Contract tests for OpenAPI and the generated TypeScript client, plus a CI drift check that fails when the committed client is stale.
- Dependency scanning and both Docker image builds in CI; GitHub Actions pinned per repository policy.
- Documentation matching the implementation: `docs/architecture.md`, `docs/api.md`, `docs/security.md`, ADRs for the decisions actually made, and a README with the documented startup steps.

**Definition of done — the global acceptance checklist from §15**

- `docker compose up --build` starts a usable local application from the documented steps on a clean clone.
- A new user can authenticate, ask a fresh-information question, see a streamed answer with working citations, reload it, and log out.
- Database migrations work on an empty database.
- All format, lint, type, unit, integration, frontend, and e2e checks pass.
- Provider calls are mocked in CI.
- Authorization tests prove cross-user conversation access is denied.
- No secrets are committed and production configuration fails closed.
- OpenAPI and the generated client are synchronized.
- README and the architecture/security docs match the actual implementation.

**Verify**

```bash
make lint && make typecheck && make test && make e2e
uv run python -m evals.run --subset ci
make generate-client && git diff --exit-code frontend/src/api/generated
# clean-clone rehearsal
git clone <repo> /tmp/rehearsal && cd /tmp/rehearsal && cp .env.example .env && docker compose up --build
```

---

## Phase 11 — Chat-scoped PDF retrieval (v2)

**Goal:** Let a user upload PDFs to one conversation and get answers grounded in them, cited by filename and page, without weakening ownership, privacy, or the run budgets. The design and every default are in [ADR-0015](docs/adr/0015-chat-scoped-pdf-retrieval.md). Phase 10 (abuse controls, trusted client IPs, backups, capacity) is recorded in ADR-0013 and ADR-0014.

Each slice below is shippable and leaves the repository runnable.

### 11a — Storage and embedding foundation

- PostgreSQL image `pgvector/pgvector:pg16` (exact version tag, at least 0.8.2) in `compose.yaml`, `compose.prod.yaml`, and both CI service blocks. Migration notes for existing volumes (dump and restore; see ADR-0015 Consequences).
- Migration: `CREATE EXTENSION vector`; `documents` and `document_chunks` tables. Downgrade path included. The migration refuses a data volume reattached from the Alpine image (collation guard in `app/db/migrations_helpers.py`).
- `EmbeddingProvider` protocol, an `AsyncOpenAI` implementation with SDK retries off and transient/permanent classification (quota-exhausted 429s are permanent), and a deterministic hashed bag-of-words fake.
- Typed settings for the embedding provider. `EMBEDDING_DIMENSIONS` must equal the vector column width.
- Operator procedure for moving existing data to the new image (`docs/deployment.md` §5), rehearsed against a real development database.

Deferred from the original 11a list: the `citations` changes move to 11c, the first slice that produces a document citation, so the public schema changes once. The readiness check for the extension was dropped (ADR-0015, "Observability and readiness").

**Done when:** migrations go up and down on an empty database; the fake and the real provider's error mapping are unit-tested with an `httpx2.MockTransport` under the real SDK; the collation guard is proven through a real `alembic upgrade`; CI is green on the new image.

### 11b — Upload and ingestion

- Embedding provider built in the lifespan and closed on shutdown; production refuses to start without `EMBEDDING_API_KEY` (added to `compose.prod.yaml` and `.env.production.example`).
- Document limits in typed settings (`MAX_DOCUMENT_BYTES`, pages, characters, chunks, documents per conversation).
- `POST/GET/DELETE /conversations/{id}/documents[/{document_id}]` with ownership at the SQL boundary, `404` for guessed IDs, verified email required, per-user upload rate limit, and per-conversation document cap.
- Upload-only body limit (API middleware, and an nginx location sharing a new `api-proxy.conf` snippet), `%PDF-` magic-byte check, streamed size cap; the body is read only after auth, rate limit, verification, and ownership pass.
- `pypdf` parsing in a child process per upload, forked from a forkserver, with timeout and memory limit, at most `DOCUMENT_PARSE_WORKERS` at once; `pdf_invalid`, `pdf_encrypted`, `pdf_no_text`, `pdf_too_large`, `pdf_too_complex`.
- Page-bounded chunking with overlap; SHA-256 duplicate detection.
- In-process ingestion manager: `SKIP LOCKED` lease, batched embedding, attempt limit, startup and periodic sweep.
- Daily embedding-token quota from an append-only `embedding_usage` ledger (migration), so deleting a document does not refund its tokens.
- Document deletion and conversation deletion hard-delete documents and call `checkpointer.adelete_thread`. `409` while a run is active.

**Done when:** integration tests cover cross-user access, duplicate upload, each rejection code, a crash between chunking and embedding (sweep resumes it), quota exhaustion, and checkpoint removal on delete; a hostile-PDF test proves a hung parse is killed.

**Delivered:** all of the above. Verified beyond the suite: nginx per-route limits exercised against a running container (300 KB passes only on the upload path, 12 MB is refused there), and two deliberate breakages (no hard delete on conversation delete, no attempt counting) each caught by the intended tests.

### 11c — Retrieval in the agent

- `decide_search` → `decide_retrieval` with `use_documents`; `document_query` added to the structured decision.
- `retrieve_documents` node: query embedding under timeout and one transient retry, hybrid vector plus full-text ranking fused with RRF, relevance floor, top-k and character budget, `D1…` labels.
- Migration: `citations` gains `kind`, `document_chunk_id` (`ON DELETE CASCADE`), `page`, and nullable `url`/`display_url` with a check constraint that web citations keep both.
- Citation validator and stream filter accept `D` labels; document citations persisted with chunk and page.
- Prompts fence document text as untrusted, add a document-versus-web disagreement rule; bump `PROMPT_VERSION`.
- SSE: `reading_documents` status, `documents.retrieved` (metadata only), `kind`/`page`/`document_id` on `citation`.
- Eval cases from ADR-0015 added to `backend/evals/dataset.json`, with retrieval recall@k reported. The relevance floor default is set from these results.

**Done when:** graph tests cover every routing combination; no path loops; an ownership test proves user B never retrieves user A's chunk with an identical question; the `ci` eval subset passes its gates.

**Delivered:** all of the above, plus the frontend half of 11d's citation work (document source cards, `[D1]` markers, the live "Read report.pdf · 2 passages" line, the temporary note removed). Retrieval quality is measured live by `evals/retrieval.py` rather than as recall@k inside the scripted eval. Findings that changed the design, all recorded in ADR-0015: keyword matches now bypass the relevance floor only for terms with a digit (plain words leaked irrelevant chunks); the floor of 0.25 is calibrated against the real model; and the answering prompt is told when document retrieval failed or found nothing, after the real model claimed it "cannot access uploaded files". The seven document eval cases pass against gpt-4o-mini as well as in CI.

### 11d — Frontend

- PDF upload control in the composer (`accept="application/pdf"`, client-side size check mirrored from the server limit), document chips with status, polling while `processing`, remove action, accessible error states. **Delivered early, ahead of 11c, so uploads can be tried by hand:** a paperclip button inside the message box, chips above the text, a backend test pinning `MAX_DOCUMENT_BYTES` in `constants.ts` to the setting. The chips carry a temporary note that answers do not use documents yet; remove it in 11c.
- Document source cards (filename and page, no external link); `rehypeCitations.ts` accepts `D` labels. **Delivered in 11c.**
- OpenAPI and the generated client regenerated and committed. **Delivered in 11c.**

**Done when:** Vitest covers upload states, polling stop conditions, and document citations; Playwright uploads a fixture PDF with fake providers, asks about it, sees a `[D1]` citation, reloads, and deletes the document.

**Delivered:** `frontend/tests/e2e/documents.spec.ts` attaches a generated PDF through the paperclip's file chooser, waits for it to be ready, gets a `[D1]` answer citing page 2, follows the marker, reloads, and removes it; a file that only claims to be a PDF is refused with the server's reason; a document does not appear in another conversation. Removing a document now refetches the thread, so its source cards disappear at once (the spec fails without it). The e2e server runs real parsing and retrieval with fake embeddings.

### 11e — Hardening and docs

- Metrics from ADR-0015 (ingestion, embedding, retrieval, empty-retrieval rate).
- `security.md`: hostile files, document prompt injection, third-party embedding, deletion and backup retention. `architecture.md` and `api.md` updated.
- Load test: upload plus concurrent streams, confirming parsing does not stall the event loop.

**Verify**

```bash
make lint && make typecheck && make test && make e2e
uv run python -m evals.run --subset ci
make generate-client && git diff --exit-code frontend/src/api/generated
```
