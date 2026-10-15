# 14. Trusted client addresses, per-account limits, backups, and a measured capacity

Date: 2026-10-11

## Status

Accepted.

## Context

The follow-up to [ADR-0013](0013-abuse-controls.md) set out to put Cloudflare
in front, add backups, and load-test. Preparing the edge exposed a real hole:

- **The per-IP limits could be bypassed.** nginx forwarded
  `X-Forwarded-For: $proxy_add_x_forwarded_for`, appending the peer to
  whatever the client sent. The API trusts that header from any peer
  (`FORWARDED_ALLOW_IPS='*'`, safe only if nginx is honest), and uvicorn, when
  every hop is trusted, takes the **leftmost** entry: the client's. A client
  sending a random `X-Forwarded-For` on every request was never limited on
  sign-in, registration, or password reset.

Separately: per-IP limits do nothing against attempts on one account spread
over many addresses; there were no backups; and capacity was an estimate.

## Decision

- **nginx resolves the client and replaces the header.** A start-up script
  (`frontend/nginx/25-real-ip.sh`) turns `TRUSTED_PROXY_CIDRS` into
  `set_real_ip_from` rules with `real_ip_recursive`, and nginx forwards
  `X-Forwarded-For: $remote_addr`, one address it worked out itself. Entries
  are validated, so a typo stops the container. Production Compose requires
  the variable, because an empty value behind a proxy would put every visitor
  in one rate-limit bucket. Verified in containers: a client's spoofed header
  is ignored, a trusted proxy's is honoured, an untrusted peer cannot pose as
  a proxy, and Cloudflare's full range list passes `nginx -t`.
- **Per-account limits** on sign-in (20 per 5 minutes, checked before any
  Argon2 work, applied to correct passwords too) and on reset emails (3 per
  5 minutes, dropped silently so the response never changes). Keyed on a hash
  of the normalized address.
- **Backups** by a `backup` service: `pg_dump -Fc` on an interval into a host
  directory, written under a temporary name and renamed when complete, mode
  600, pruned by age, and a health check that turns the container unhealthy
  when no recent dump exists. `restore.sh` validates the archive first and
  restores in one transaction. Drilled against real data: identical row
  counts in every table including checkpoints, a repeat restore works, and a
  truncated dump is refused before any change.
- **Cloudflare is documented, not automated** (`docs/deployment.md`): it needs
  the owner's account and domain. The trusted-proxy work above is what makes
  the application correct behind it.
- **A load-test harness** (`tests/load/`) runs the real app as one process
  with providers that wait like real ones. Results are in
  `docs/load-testing.md`.

## Consequences

- Operators must set `TRUSTED_PROXY_CIDRS` (usually `172.16.0.0/12` for a proxy
  on the same host, plus Cloudflare's ranges behind Cloudflare).
- An attacker can keep one account's password sign-in throttled. Its existing
  sessions keep working and a password reset still gets through.
- Dumps on the server's own disk protect against mistakes, not a lost server;
  copying them off the machine is the operator's step (documented).
- One API process carries about 50 simultaneous answer streams comfortably
  and saturates near 700 answers a minute on one CPU core. Scale with
  replicas; the coordination already supports it.
- Measured: before ADR-0013's hashing fix, 5 sign-ins a second cut answer
  throughput by 72%. With it, the effect is negligible.
- Not done: MFA (a feature with product decisions of its own) and a
  third-party security review.
