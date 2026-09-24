# Security

Status: reflects Phase 5 (agent core). Streaming controls land in Phases 6–7.

## Authentication

### Passwords

Hashed with **Argon2id** (`argon2-cffi` defaults, tracking RFC 9106). Minimum
12 characters, maximum 1024 — Argon2 has no bcrypt-style truncation, so long
passphrases are genuinely used, and the ceiling exists only to bound the
hashing work an anonymous caller can demand. Passwords are never logged.

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

Starting a run is limited **per user** (20 per minute by default,
`RATE_LIMIT_RUNS`): it is the path that spends provider money, and a per-IP
limit would let one account on a shared network starve the others.

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

Deletion is soft: the row is stamped `deleted_at` and disappears from every read
path. Deleting a conversation also cancels its active run. Rows therefore accumulate; a retention/purge policy is a later concern and
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

## Request limits

Bodies over `MAX_REQUEST_BODY_BYTES` (256 KiB) are refused with
`413 payload_too_large` before they are parsed. A declared `Content-Length`
is checked without reading the body, and a chunked body is counted as it
arrives. The limit is sized so that any valid message fits however it is
encoded. The worst case, 16,000 emoji ASCII-escaped by the client, is about
192 KB. Other bounds: message length (16,000 characters), output tokens,
results per search, per-search and per-run timeouts, and the database pool
size and checkout timeout.

## Not yet implemented

Email verification (`is_verified` exists but is unused), and a purge
policy for soft-deleted conversations and their runs.

## Reporting

Do not open a public issue for a suspected vulnerability. Contact the
maintainers directly.
