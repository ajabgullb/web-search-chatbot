# Security

Status: reflects Phase 3 (authentication frontend). Agent and streaming
controls land in Phases 5–7.

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
  bundle). HSTS is added in production.
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

The Phase 2 limiter is **in-process**: with N replicas the effective limit is
N x the configured value. Phase 8 replaces it with a Redis-backed limiter
behind the same interface.

## Error handling

Every failure returns one envelope:

```json
{"error": {"code": "...", "message": "...", "request_id": "...", "details": null}}
```

Clients switch on `code`. Stack traces, driver messages, and provider payloads
never reach a client; unhandled exceptions are logged server-side and reported
as `internal_error` with the request id the caller can quote.

## Configuration fails closed

Booting with `ENVIRONMENT=production` is refused when `JWT_SECRET` is a known
placeholder or under 32 characters, or when `FRONTEND_ORIGIN` contains a
wildcard or is not `https`. `DATABASE_URL` must use the async driver. Phase 8
extends these checks; they are never relaxed to make an environment start.

## Not yet implemented

Email verification (`is_verified` exists but is unused), CSP tuned to the
frontend, distributed rate limits, log redaction filters, and the agent-side
controls for untrusted web content.

## Reporting

Do not open a public issue for a suspected vulnerability. Contact the
maintainers directly.
