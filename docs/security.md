# Security

Status: reflects the complete v1. The end-to-end suite exercises the
ownership, session, and link-safety controls below in a real browser.

## Authentication

### Passwords

Hashed with **Argon2id** (`argon2-cffi` defaults, tracking RFC 9106). Minimum
12 characters, maximum 1024 — Argon2 has no bcrypt-style truncation, so long
passphrases are genuinely used, and the ceiling exists only to bound the
hashing work an anonymous caller can demand. Passwords are never logged.

Argon2 is deliberately slow (100-300 ms of CPU per hash), so it never runs on
the event loop: a hash there would freeze every open answer stream in the
process for its duration, and a few logins a second would stall the API. The
password functions are async and run in a two-thread pool (argon2-cffi
releases the GIL). Two threads, because each hash already uses four lanes and
64 MiB; more threads would add memory without throughput, and a login flood
queues instead of exhausting the container.

### Email verification

Registration emails a link carrying a **signed** token (same key as access
tokens, `typ: email_verification`, 48 h expiry) rather than a stored one. It
only proves control of an inbox, and a second use is harmless, so a database
row would add nothing. Each decoder insists on its own `typ`, so the link is
not a working API credential and an access token cannot confirm an address.
The token names the address it was sent to and is refused if the account's
address differs.

Unverified accounts may sign in and manage conversations but **cannot start
runs** (`403 email_not_verified`): runs spend provider money, and accounts on
addresses nobody controls are otherwise free to create. Production refuses
`REQUIRE_EMAIL_VERIFICATION=false`. Resending is limited per account, not per
IP, because what it protects is the inbox the mail goes to. A failed send at
registration is logged and does not fail the registration. See
[ADR-0013](adr/0013-abuse-controls.md).

### Tokens

| | Access token | Refresh token |
| --- | --- | --- |
| Format | Signed JWT (HS256) | Opaque, 48 random bytes |
| Lifetime | 15 minutes | 30 days |
| Storage | Client memory only | `Secure`, `HttpOnly`, `SameSite=Lax` cookie scoped to `/api/v1/auth` |
| At rest | Not stored | SHA-256 hash only |

The signing key must be at least 32 characters (RFC 7518 §3.2 for HS256);
shorter values are rejected at startup. Decoding pins the algorithm list, so
`alg: none` and HS/RS confusion attacks fail. Issuer, audience, expiry, and a
`typ: access` claim are all verified, and the user is re-read from the database
on every request so a deactivated account loses access immediately rather than
at the end of the token's lifetime.

Two hashing strategies are used deliberately: Argon2id for low-entropy,
user-chosen passwords, and SHA-256 for high-entropy random tokens, where brute
force is not the threat and the hash must double as an indexed lookup key.

### Refresh rotation and reuse detection

Every refresh issues a new token and retires the presented one. All tokens
descended from one login share a `family_id`.

If an already-retired token is presented, the server cannot tell whether it was
stolen or replayed — so it revokes **the entire family**. Every device for that
login must sign in again. Losing the session is the correct trade: the
alternative is serving an attacker silently.

### Password reset

- Single-use tokens, hashed at rest, expiring in 30 minutes.
- Requesting a new reset invalidates any outstanding one, so a mailbox never
  accumulates working credentials.
- `POST /auth/forgot-password` returns `202` with an identical body whether or
  not the address is registered, and only sends mail when it is.
- Completing a reset revokes every session for the account.
- Login answers identically for an unknown account and a wrong password, and
  spends the same hashing work in both cases so response time cannot be used
  to enumerate accounts.

The one place existence *is* disclosed is registration, which returns `409
email_already_registered`. Silently accepting a duplicate would be worse for
users; rate limiting blunts enumeration through this route.

## Transport and browser controls

- **CORS**: explicit origins from `FRONTEND_ORIGIN`, credentials enabled, never
  a wildcard.
- **Origin validation** on the cookie-authenticated state-changing routes
  (`/auth/refresh`, `/auth/logout`, `/auth/logout-all`). `SameSite=Lax` already
  blocks the cross-site POST that CSRF needs, but it is a browser-side control
  with historical gaps; this is a second lock on the same door. Requests with
  no `Origin` header at all are allowed, since a non-browser client cannot be a
  confused deputy.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive
  `Permissions-Policy`, `Cache-Control: no-store`, and
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none';
  base-uri 'none'` (relaxed only for the Swagger docs page, which loads a CDN
  bundle, and which production does not serve). HSTS is added in production.
- **The frontend's own CSP** is set by its nginx (`frontend/nginx/`):
  `default-src 'self'`, with no `'unsafe-inline'`, no `'unsafe-eval'`, and no
  third-party hosts, because the built app needs none of them. `img-src` allows
  only this origin and `data:`. A Markdown image in an answer that points at an
  attacker's server is blocked, which closes a known channel for exfiltrating
  data through prompt injection. The policy was checked by rendering every
  public route of the production image in headless Chrome: no violations
  (a positive control confirmed that violations are detected). The same
  config adds `nosniff`, frame denial, a referrer policy, COOP, and HSTS when
  the request came over TLS (`X-Forwarded-Proto: https`). TLS itself is
  terminated in front of this nginx; see the deployment notes in the README.
- **Browser storage is off-limits for auth.** ESLint fails the build on
  `localStorage` and `sessionStorage`, in both the bare-global and `window.`
  spellings. The access token is a module variable in `src/api/client.ts` and
  is gone when the tab closes. The single documented exception is the light/dark
  theme preference, which carries an explicit `eslint-disable` and a reason.
- **One refresh at a time.** The client coalesces concurrent 401s into a single
  refresh. Firing several would present an already-rotated token and trip the
  server's reuse detection, logging the user out of every device — a
  self-inflicted denial of service.
- **No retry loops.** A request is replayed at most once, and only if the
  refresh produced a token. A dead session surfaces the original 401.

## Rate limiting

Per-IP sliding windows on `login` (10), `register` (5), and `forgot-password`
(5), each over 5 minutes, returning `429` with `Retry-After`.

**Which IP.** The API takes the client address from `X-Forwarded-For`, so that
header must not be the client's to write. Until 2026-10-11 nginx *appended* to
whatever the client sent and uvicorn, trusting every hop, read the leftmost
entry: a client could name a fresh address on every request and never be
limited. nginx now resolves the client itself, believing `X-Forwarded-For`
only from addresses in `TRUSTED_PROXY_CIDRS`, and *replaces* the header with
that one address (`frontend/nginx/25-real-ip.sh`). Verified in containers: a
spoofed header from a client is ignored, a trusted proxy's is honoured, and an
untrusted peer cannot pose as a proxy. See `docs/deployment.md`.

**Per account as well.** Per-IP limits do nothing against a botnet spreading
guesses at one account over thousands of addresses. Sign-in is also limited
per email address (`RATE_LIMIT_LOGIN_PER_ACCOUNT`, 20 per 5 minutes), checked
before any Argon2 work and applied to a correct password too, so a 429 never
confirms a guess. Reset emails are capped per address
(`RATE_LIMIT_FORGOT_PASSWORD_PER_ACCOUNT`, 3 per 5 minutes) *silently*: the
response is unchanged, so the cap cannot be used to probe accounts. Keys are a
SHA-256 of the normalized address, so Redis holds no email addresses. The
trade-off of any per-account limit is that an attacker can keep one account's
password sign-in throttled; its existing sessions keep working and a reset
still gets through.

Starting a run is limited **per user** (20 per minute by default,
`RATE_LIMIT_RUNS`): it is the path that spends provider money, and a per-IP
limit would let one account on a shared network starve the others.

A per-minute limit alone still allows ~28,800 runs a day to a patient script,
so each user also has a **daily quota** (`DAILY_RUN_LIMIT_PER_USER`, 100 runs
in any rolling 24 hours), refused with `429 daily_quota_exceeded`. It is
counted from the `runs` table (indexed on `user_id, created_at`), so it holds
across replicas and survives a Redis restart. Runs in deleted conversations
still count, so deleting a thread cannot refund spent budget. It is a
check-then-insert: submissions racing in different conversations can overshoot
by the number in flight, which the per-minute limit bounds. An idempotent
replay of an accepted request is answered even at the limit.

With `REDIS_URL` set (required in production) the windows live in Redis and
are shared by every replica. Each check is one Lua script: it drops expired
entries, counts, and records the attempt atomically, using Redis's clock so
replicas with drifting clocks agree. Only allowed attempts are recorded, so a
client that keeps hammering does not extend its own ban. Without Redis the
limiter is in-process, and with N replicas the effective limit is N x the
configured value.

If Redis becomes unreachable, the limiter **degrades to per-process limits**
and logs `rate_limit_degraded`. Failing closed would lock every user out of
login during an outage. Failing open would remove brute-force protection
entirely. Per-process limits are weaker, but they are still limits.

## Error handling

Every failure returns one envelope:

```json
{"error": {"code": "...", "message": "...", "request_id": "...", "details": null}}
```

Clients switch on `code`. Stack traces, driver messages, and provider payloads
never reach a client; unhandled exceptions are logged server-side and reported
as `internal_error` with the request id the caller can quote.

## Logging

Logs are JSON, one object per line (`LOG_FORMAT=console` gives a readable
line for local work). Each line has a stable `event` name and explicit
fields. `app/core/logging.py` adds the correlation fields: `request_id`, an
opaque `user` reference, and a run's `run_id` and `conversation_id`. The user
reference is a truncated SHA-256 of the user id: log readers cannot turn it
back into an account id, but someone holding an id can compute it and search.

The policy is that nothing logs prompts, message bodies, search queries, page
content, passwords, tokens, cookies, or authorization headers. Redaction is
the safety net under it:

- Fields named like secrets or content (`password`, `token`, `content`,
  `snippet`, `query`, …) are written as `[REDACTED]` whatever they hold.
- Messages and tracebacks are scrubbed of bearer tokens, JWTs, provider-key
  shapes, `password=`-style pairs, and email addresses. This covers driver
  errors such as a unique violation that echoes an email.

Uvicorn's access log is replaced by one `http_request` line per request with
method, path, status, and duration. Health probes and metric scrapes log at
DEBUG. `httpx` and `openai` are held at WARNING, so provider URLs are not
logged on every call. `tests/integration/test_log_hygiene.py` drives a whole
session (register, login, refresh, a searched and cited run, logout) and
fails if any credential or piece of content appears in the output.

## Tracing

LangSmith tracing is off unless `LANGSMITH_TRACING=true`
(`app/agent/tracing.py`). When it is on, a tracer is attached to each run
explicitly. Inputs and outputs are replaced with empty payloads inside the
process, before upload, so prompts, conversation turns, search results, and
answers never leave. Traces keep structure, timings, token counts, errors,
the `env:` and `prompt:` tags, and metadata: environment, prompt and graph
versions, and run id. User and conversation are left out, so a trace cannot
be tied to a person's activity.

LangChain's implicit tracing, which a `LANGSMITH_TRACING` variable in the
container environment would switch on with full content, is forced off at
startup. The app owns that decision.

A unit test captures the bytes the LangSmith client would send for a
searched, cited run and checks that the question, the page text, and the
answer are absent. With hiding disabled, the same test sees them. An
unreachable LangSmith does not slow runs, and the app's shutdown gives up on
the final flush after 5 s. The SDK's own exit handler may still retry for a
few seconds before the process ends.

## Authorization and data isolation

Every conversation and message query is constrained by the authenticated
`user_id` in SQL, at the repository boundary. There is no "fetch by id" a
service can call and then forget to check.

A conversation that belongs to someone else and one that never existed give
**identical** responses: `404` with code `conversation_not_found`, the same
message, the same shape. A `403` would confirm that a guessed UUID names a real
row, turning every endpoint into an enumeration oracle. Integration tests assert
the two responses match, for `GET`, `PATCH`, `DELETE`, and message listing.

Runs follow the same rule: `GET /runs/{id}` and `POST /runs/{id}/cancel` are
scoped by `user_id` *and* by the conversation still being live, so another
user's run, a guessed id, and a run in a deleted conversation all answer
`404 run_not_found`. A retry must name a user message in the caller's own
conversation; a message id taken from another user's thread is `404`.
Idempotency keys are scoped per user, so one client's key can never collide
with, or replay, another user's run.

Documents follow the same rule, scoped by `user_id` and by conversation:
another user's document, and a real document id requested under the wrong
conversation, both answer `404`.

Deletion is soft: the row is stamped `deleted_at` and disappears from every read
path. Uploaded documents are the exception: they are hard-deleted (see
[Uploaded documents](#uploaded-documents)). Deleting a conversation also cancels its active run. Rows therefore accumulate; a retention/purge policy is a later concern and
is not implemented.

## Untrusted content rendering

Assistant messages are rendered as Markdown with raw HTML disabled,
`rehype-sanitize` applied, and link schemes restricted to `http`/`https` with
`rel="noopener noreferrer"`. There is no `dangerouslySetInnerHTML` anywhere in
the frontend. Tests cover `<script>`, `<img onerror>`, `javascript:`, `data:`,
and `file:` payloads. This matters because answers paraphrase fetched web
pages. See
[ADR-0006](adr/0006-markdown-rendering.md).

User messages are rendered as plain text, never parsed as Markdown.

Citations add no new injection path. Markers become buttons in a plugin that
runs *after* sanitizing, on text the sanitizer already accepted, and are
skipped inside code and links. Source links are rendered only for absolute
`http`/`https` URLs, with `target="_blank"` and `rel="noopener noreferrer"`;
anything else shows its title as text. A test scans the application source and
fails if `dangerouslySetInnerHTML` is ever set.

## Agent and untrusted web content

Retrieved web text is hostile input (OWASP LLM01). The controls below bound what
injected text can *do* — choose a tool argument, fabricate a citation, reach a
secret — not what the model can be persuaded to *say*. An answer can still be
wrong because a page lied; that is why answers carry citations the user can check.

- **A declared data boundary.** The synthesis system prompt states that search
  results are untrusted data that cannot change instructions, request secrets,
  or authorize tools. Each snippet is additionally fenced as
  `<<<untrusted-excerpt … >>>` so the boundary is visible in the token stream,
  not only asserted. Only provider snippets are used, capped at 1,200
  characters; page bodies are never requested.
- **Tool arguments never come from retrieved content.** Queries come only from
  the user's own message (heuristic path) or from the structured decision made
  *before* any search runs. They are validated — whitespace collapsed, 400
  characters, deduplicated, count-capped — and the graph has no back edge, so
  result text cannot trigger another search.
- **Citations are verified, not trusted.** The model may cite only the `S1…Sn`
  labels we assigned. Unknown labels are stripped from the answer; a direct
  answer has every marker stripped; a searched answer left with no valid
  citation is flagged `degraded` with a user-facing notice.
- **URLs are validated before they can be rendered.** Only `http`/`https`;
  control characters rejected; tracking parameters dropped; canonicalized for
  deduplication. The frontend re-checks schemes at render time regardless.
- **Secrets stay out of state and logs.** Provider clients and credentials live
  in runtime context and are never checkpointed — an integration test asserts a
  stored checkpoint contains neither. Prompts contain no secrets. Provider error
  bodies, which can echo a key or a query, are never logged or propagated; logs
  record the exception class and HTTP status only.
- **Budgets.** At most 3 searches, 5 results each, 20 s per search, 90 s per run,
  1,500 output tokens, and 20 history messages — all configurable. Exactly one
  retry, for transient failures only; the OpenAI SDK's own retries are disabled
  so the retry budget has a single owner.

Covered by `tests/unit/agent/test_prompt_injection.py`,
`tests/unit/agent/test_citations.py`, `tests/unit/agent/test_urls.py`, and
`tests/integration/test_checkpointer.py`. See [ADR-0007](adr/0007-agent-core.md).

## Streaming

What leaves the server during a run is decided in one place: internal agent
events are mapped to wire events by `RunManager._to_public`, and anything a node
writes that is not one of our event types is dropped before it can be streamed.

- **No reasoning.** `agent.status` is a coarse phase label (`thinking`,
  `searching`, `writing`). Rationales for the search decision are log-only.
- **No page text in progress events.** `search.completed` carries source
  metadata only. A snippet reaches the client only inside a `citation`, the one
  place a retrieved excerpt is meant to be shown — and it is rendered as
  sanitized Markdown like every answer.
- **No invented citations on screen.** Deltas pass through the same citation
  validation as the saved answer, holding back a partial `[S…` only until it can
  be checked, so an unknown label is never displayed even briefly.
- **No raw errors.** Failures reach the stream as a stable code and a message we
  wrote; only the code is stored. Provider exception text never reaches a
  client or the database.
- **No prompts or secrets.** An integration test scans a whole stream for the
  system prompts, the untrusted-content fence, a configured API key, and an
  injected instruction outside citation events.

Bounded by: the message length limit (enforced server-side), the output-token
limit, the run deadline, the per-user run rate limit, and one active run per
conversation. A run whose client disconnects and does not reconnect within
`RUN_ABANDON_GRACE_SECONDS` (10 s) is cancelled, so an abandoned tab does not
keep spending; only the run's owner can renew it, because the renewing poll is
ownership-checked first.

Run endpoints authenticate with the in-memory bearer token, not the refresh
cookie, so they need no `Origin` check: a cross-site page cannot attach the
token.

## Configuration fails closed

Booting with `ENVIRONMENT=production` is refused when:

- `JWT_SECRET` is a known placeholder or under 32 characters;
- `FRONTEND_ORIGIN` contains a wildcard or is not `https`;
- `LLM_API_KEY`, `SEARCH_API_KEY`, `REDIS_URL`, or `SMTP_HOST` is missing;
- `DATABASE_URL` uses a stock password (`app`, `postgres`, `password`, …);
- `METRICS_TOKEN` is set but under 32 characters;
- `SMTP_SECURITY=none`, because a reset email carries a working credential and
  must not travel to the relay in plaintext;
- `LOG_LEVEL=DEBUG` or `LOG_FORMAT=console`, which are development conveniences.

`DATABASE_URL` must use the async driver in every environment. All problems
are reported at once, as a plain list that names each setting and never its
value. Pydantic's default error embeds the rejected input, which here is the
environment and its secrets, so the app catches it and exits with only the
messages. Production also serves no `/docs` and no `/openapi.json`.
`tests/integration/test_prod_config.py` starts real processes to check each
rule and to check that no secret appears in the output. These checks are never
relaxed to make an environment start.

## Uploaded documents

PDF upload ([ADR-0015](adr/0015-chat-scoped-pdf-retrieval.md)) is the one
place the API accepts a file, and a PDF is attacker-controlled input to a
complex parser.

- **Checked before the body is read.** The upload handler does not declare
  the file as a FastAPI parameter, which would have the whole body read and
  spooled before any dependency ran. Authentication, the per-user upload rate
  limit, email verification, and conversation ownership are all checked first.
- **Size limits on one route only.** nginx (`client_max_body_size 11m`) and
  the API's body-size middleware raise the limit for `POST` on the upload path
  and nowhere else; every other route keeps 256 KiB.
- **The bytes decide.** A file is a PDF only if `%PDF-` appears in its first
  1024 bytes. The declared content type and the file name are ignored.
- **Parsed in a disposable process.** Each parse runs in a child process
  forked from a forkserver that has loaded nothing but `pypdf`: no settings,
  no secrets, no open sockets. The child has an address-space limit
  (`DOCUMENT_PARSE_MEMORY_MB`) and is killed at
  `DOCUMENT_PARSE_TIMEOUT_SECONDS`. A hang, a crash, or a memory blow-up is
  reported as `pdf_too_complex`; none of them reaches the API process. At most
  `DOCUMENT_PARSE_WORKERS` parses run at once, and a further upload gets `503`
  rather than queueing in memory.
- **No file is kept.** The bytes exist only for the request (Starlette spools
  a multipart file over 1 MiB to a temporary file, deleted when the request
  ends). Only extracted text is stored, as chunks.
- **Names are data.** The filename is reduced to a base name with control
  characters removed: it is shown to users and will reach the model as a
  source title, where a newline could break the untrusted-content fence.
- **Spend is capped per user**, in tokens per rolling 24 hours, from an
  append-only ledger (`embedding_usage`). The ledger is not linked to
  documents, so deleting and re-uploading does not reset it.
- **Deletion is real.** Documents are hard-deleted, never soft-deleted, with
  their chunks. Deleting a conversation hard-deletes its documents in the same
  transaction. Both also erase the conversation's LangGraph checkpoints
  through the checkpointer's own `adelete_thread`, because retrieved text will
  pass through graph state. Deleted text remains in database backups until
  they rotate out (`BACKUP_KEEP_DAYS`).
- **A third party sees the text.** Document text is sent to OpenAI's
  embeddings endpoint, and queries will be too.
- **Keys stay with their provider.** With no `EMBEDDING_API_KEY`, the chat
  key is reused only if the chat endpoint is on the embeddings host. A Groq or
  OpenRouter key is never sent to OpenAI.
- **No deadlock with deletes.** A batch of embeddings locks the document row
  before its chunks, the order a delete takes them in. The reverse order
  deadlocked against a concurrent delete, which then failed with a 500; a
  test reproduces that interleaving deterministically.

## Request limits

Bodies over `MAX_REQUEST_BODY_BYTES` (256 KiB) are refused with
`413 payload_too_large` before they are parsed. A declared `Content-Length`
is checked without reading the body, and a chunked body is counted as it
arrives. The limit is sized so that any valid message fits however it is
encoded. The worst case, 16,000 emoji ASCII-escaped by the client, is about
192 KB. Other bounds: message length (16,000 characters), output tokens,
results per search, per-search and per-run timeouts, and the database pool
size and checkout timeout. The PDF upload route is the one exception; see
[Uploaded documents](#uploaded-documents).

## Not yet implemented

A purge policy for soft-deleted conversations and their runs, multi-factor
authentication, and a third-party security review.

## Reporting

Do not open a public issue for a suspected vulnerability. Contact the
maintainers directly.
