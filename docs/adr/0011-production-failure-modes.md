# 11. Production hardening: how each dependency is allowed to fail

Date: 2026-09-24

## Status

Accepted.

## Context

Phase 8 added Redis, metrics, tracing, and email that is actually delivered.
Each is a dependency that can be down, slow, or misconfigured, and "what
happens then" had to be decided per dependency rather than by one blanket
rule. Failing closed everywhere would let a tracing outage stop chat. Failing
open everywhere would let a Redis outage disable brute-force protection
without anyone noticing.

## Decision

Each dependency fails in the way that is safest *for what it protects*:

| Dependency | When it fails | Why |
| --- | --- | --- |
| Configuration (production) | **Refuse to start**, listing every problem, never echoing values | A misconfigured production that boots looks healthy until it leaks. The values are hidden because pydantic's default error embeds the environment. |
| Redis run lock | **Fail closed**: `503` on run creation | Production is configured to coordinate through Redis. The database index still prevents a double run, but hiding the outage is worse ([ADR-0010](0010-redis-run-lock.md)). |
| Redis rate limiter | **Degrade** to per-process limits and log `rate_limit_degraded` | Closed would lock everyone out of login. Open would drop brute-force protection. Weaker limits are still limits. |
| Redis cancel signal | **Best effort** | The database records every cancel durably. A lost signal only costs promptness: the remote model call runs to the end. |
| LangSmith | **Invisible to users**: background upload, shutdown gives up after 5 s | Tracing is diagnostic. It must never cost a user an answer. |
| SMTP | **TLS required** in production; delivery failures reach the caller | A reset email carries a working credential. |
| Metrics endpoint | **Closed** in production without `METRICS_TOKEN` | Metrics reveal traffic and failing dependencies. |
| Shutdown | **Drain**, then interrupt as retryable | Uvicorn drains open streams (100 s). The app drains runs without a stream (`SHUTDOWN_DRAIN_SECONDS`). Whatever is left fails as `run_interrupted`, which the client may retry. |

Two supporting decisions:

- **Logging is explicit and redacted.** Events have names and explicit
  fields. Redaction by field name and by credential shape is a safety net
  under a no-content policy, and it is tested by scanning a whole session's
  output.
- **One environment definition for the API and its migration job**
  (`compose.prod.yaml`), so the migration runs with exactly the validated
  settings the API will.

## Consequences

- A Redis outage stops new chats (`503`), keeps login working with weaker
  limits, and slows cross-replica cancels. Readiness reports it.
- Operators must provide real SMTP credentials with TLS, a strong database
  password, and a metrics token before production will start.
- Verified end to end on the production images: migrations as a one-shot
  job, non-root containers, hidden docs, a token-guarded `/metrics`, Redis
  rate limits keyed on the real client IP through the proxy (for honest
  clients; a spoofed `X-Forwarded-For` was still believed until
  [ADR-0014](0014-edge-backups-and-capacity.md) fixed it), JSON logs with no
  secrets, and a clean graceful stop.
