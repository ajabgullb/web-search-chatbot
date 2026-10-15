# 13. Abuse controls: off-loop hashing, email verification, daily quota

Date: 2026-10-11

## Status

Accepted.

## Context

A review of what v1 could withstand found three cheap ways to hurt it:

1. **Argon2 on the event loop.** Every login or registration hashed a password
   synchronously inside an async handler. Measured on a development machine:
   one verification takes 100-300 ms and stalls *every* request in the
   process for that long. A few logins a second, from many IPs to get past
   the per-IP limit, would freeze all open answer streams.
2. **Accounts are free.** `is_verified` existed but nothing checked it. Any
   address, real or not, could register and start spending provider credit.
3. **No ceiling on spend per account.** The only run limit was 20 a minute,
   which a patient script stays under for ~28,800 runs a day.

## Decision

- **Hash in a bounded thread pool.** `hash_password` and `verify_password`
  are async and run in a dedicated two-thread executor. argon2-cffi releases
  the GIL, so the loop keeps serving; we measured the worst loop stall falling
  from ~176 ms to the timer's own resolution. Two threads, not the default
  pool: each hash already uses four lanes and 64 MiB, so more threads add
  memory, not throughput. A flood queues rather than exhausting memory.
- **Verified email before runs.** Registration emails a link with a signed
  token (`typ: email_verification`, 48 h). Signed, not stored: it proves only
  control of an inbox and reuse is harmless, so a table and migration would
  add nothing. It names the address, so it fails if the address changes.
  Unverified accounts can sign in and organise conversations; `RunService`
  refuses runs with `403 email_not_verified`. Production refuses to start
  with `REQUIRE_EMAIL_VERIFICATION=false`. Existing accounts are not
  grandfathered: they confirm through the banner's "Resend email".
- **Daily quota from the database.** `DAILY_RUN_LIMIT_PER_USER` (100) runs in
  any rolling 24 hours, counted from `runs` with a new
  `(user_id, created_at)` index. Not Redis: the count is durable, shared by
  every replica, and cannot be reset by a Redis restart. Checked after the
  idempotency replay, so a retried, already-accepted request still gets its
  answer.

## Consequences

- Registration now sends an email. A delivery failure is logged and does not
  fail the registration; resending reports `503` instead.
- Development needs Mailpit (already in `compose.yaml`) to confirm accounts.
  The e2e server keeps sent mail in memory behind a test-only route.
- The quota is a check-then-insert and can overshoot by the number of runs a
  user starts concurrently in different conversations. The per-minute limit
  bounds that. Exactness would need serialising every submission per user.
- Not addressed here: per-account login throttling, MFA, volumetric DDoS
  protection (an edge concern), and a measured capacity figure (needs a load
  test).
