# 10. An application-level run lock in front of the database index

Date: 2026-09-24

## Status

Accepted. Extends [ADR-0008](0008-runs-and-streaming.md): the database is still
the coordinator of record.

## Context

`AGENTS.md` §10 asks for "database/Redis coordination so at most one active run
writes to a conversation at a time". Phase 6 met that with a partial unique
index on `runs(conversation_id) WHERE status IN ('queued','running')`. The index
is correct under any interleaving and across replicas, and a crashed process
cannot hold it forever because the stale-run sweep fails old rows.

Phase 8 introduces Redis. We decided to also enforce the rule at the
application level, while keeping the index.

What the index alone cannot do:

- It rejects a conflicting submission only at commit. By then the request has
  opened a transaction, staged a user message, flushed it, and inserted a run,
  all of which the IntegrityError rolls back.
- It says nothing about a run that is being accepted but has not committed.
  Concurrent duplicates wait on the index's row lock to learn the outcome.
- Cancelling a run executing on another replica frees the index at once, but
  nothing else in the application sees that.

## Decision

**A lease per conversation, named by the run.** `app/core/run_lock.py` exposes
`acquire / release / holder` behind a `RunLock` protocol with two
implementations: `RedisRunLock` (production, all replicas) and `InMemoryRunLock`
(tests, single-process development). The Redis key is
`run-lock:conversation:{id}`. Its value is the run id, which the service
generates before the row exists, and it is taken with `SET NX PX` in one atomic
step.

**Release names the run.** A Lua script deletes the key only if it still holds
that run's id, so any process can release on the run's behalf and a late or
duplicate release is a no-op. It releases in four places:

| Who | When |
| --- | --- |
| `RunService.create` | Any path that did not commit the run: a conflict, a rejected retry, a client that hung up |
| `RunManager._execute` | In `finally`, after the outcome is written |
| `RunService.cancel` | After the cancel commits, so a run on another replica frees the conversation now, not when its executor notices |
| stale sweep | For every run it expires |

**The lease outlives any healthy run.** Its TTL is `RUN_TIMEOUT_SECONDS +
RUN_STALE_GRACE_SECONDS`, the same bound the stale sweep uses. A process that
dies holding the lock blocks its conversation no longer than its dead run row
already did.

**The database stays the source of truth.** The index is unchanged. If the lock
is lost (Redis restart or flush, a lease expiring early), the next submission
passes the lock and the index rejects it as before, with a `409`. The rejected
request gives its own lease back.

**Fail closed on a Redis outage.** If `acquire` cannot reach Redis, run creation
returns `503 service_unavailable`. It does not fall back to the database alone.
Production is configured to coordinate through Redis. Silently running without
it would hide an outage that also affects rate limiting, which moves to Redis
later in Phase 8. Release and holder lookups never raise, because they run in
cleanup paths and the lease expires on its own.

**Redis is required in production and optional in development.** An empty
`REDIS_URL` selects the in-memory lock. Production settings reject an empty one.
Readiness checks Redis only when it is configured.

## Consequences

- A conflicting submission is refused before any database write, and a `409`
  can name a run that is still being accepted (`details.run_id` falls back to
  the lock holder).
- Every run start costs one Redis round trip, and every run end costs one more.
- Run creation now depends on two systems. A Redis outage stops new runs
  (`503`) even though PostgreSQL alone could have kept them correct. That is
  the price of failing closed.
- There are two sources of "is this conversation busy", and they can disagree
  briefly. When they do, the lock can only refuse what the database would
  allow. It never allows what the database would refuse. The one case where it
  refuses more than it should: a process that crashes between `acquire` and
  commit leaves a lease with no run behind it, which blocks the conversation
  until the TTL expires. The `409` then carries a `run_id` that
  `GET /runs/{id}` does not find.
- Tests exercise both implementations. The Redis one runs against fakeredis,
  which executes the real commands and the Lua script, so CI needs no Redis
  service.
