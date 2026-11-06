# 17. MCP servers for development: Postgres, Prometheus, Playwright

Date: 2026-11-06

## Status

Accepted.

## Context

Coding agents working on this repository debug by guessing at the database,
the metrics, and the frontend, or by asking a human to look. The Model
Context Protocol (MCP) lets an agent reach those systems through tools it can
call. The product owner asked for three: PostgreSQL, Prometheus, and
Playwright.

This is **development tooling**. It isn't the product exposing an MCP server
or the agent calling MCP tools, both of which would change the architecture
and need their own decision. Nothing here ships in an image or runs in
production.

The risk is the database. A development database holds real-looking
conversations, documents, emails, and password hashes, and an agent's tool
output can end up in a model provider's logs.

## Decision

- **A committed `.mcp.json`**, so every clone gets the same servers. Each runs
  over **stdio**, so none opens a network port. Each is launched through
  `scripts/mcp/<name>.sh`, which reads ports and passwords from `.env` (data,
  never executed), because Claude Code doesn't read `.env`.
- **PostgreSQL through MCP Toolbox for Databases, logged in as
  `grafana_reader`.** We chose Toolbox because it's released actively;
  `postgres-mcp` hadn't published a release since May 2025, and the original
  reference Postgres server is archived. Toolbox's `execute_sql` runs anything,
  so the boundary has to be the database role. We reuse `grafana_reader`
  (ADR-0016) rather than adding a role: it already says "may count, may not
  read what anyone wrote", PostgreSQL enforces it, and a test covers it. The
  MCP pool takes at most 2 of the role's 4 connections, leaving Grafana room.
- **Prometheus through `prometheus-mcp-server`.** It only reads, and our
  metrics carry no per-user labels.
- **Playwright through `@playwright/mcp`, with an in-memory profile.** Each
  session starts logged out and leaves no refresh cookie on disk.
- **Exact versions pinned** in the launchers, as Compose pins its images.
- **`make mcp-check`** starts each server from `.mcp.json` over stdio, makes
  one real call to each, and asserts that `grafana_reader` is refused message
  content. Running it also downloads the servers ahead of time.

## Consequences

- The Postgres server needs the `observability` profile, which is what creates
  `grafana_reader`. Without it, the server fails with an authentication error.
- An agent can't read message content or document text through MCP. Questions
  that need it have to go through the API as a test user. That's deliberate.
- Widening the role's grants for a Grafana panel also widens what the agent
  can see. Grant changes stay reviewed in one file,
  `observability/postgres/grafana_reader.sql`.
- Playwright MCP includes `browser_run_code_unsafe` and has no way to disable
  it. Permission prompts in the agent are the control.
- `make mcp-check` isn't in CI: it needs the full stack, a browser, and
  network downloads. It's a local preflight, like `make check-db`.
- Pinned versions go stale without Dependabot, so they're bumped by hand.
