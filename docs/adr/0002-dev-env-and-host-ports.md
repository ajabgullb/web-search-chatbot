# 2. One root `.env`, with overridable Compose host ports

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

`AGENTS.md` §11 requires typed settings from the environment and a committed
`.env.example`. Two practical problems followed:

1. The backend runs from `backend/` but the shared `.env` belongs at the
   repository root, where Compose also reads it. Loading by working directory
   would make behaviour depend on where a command was launched.
2. PostgreSQL on `5432` and an API on `8000` collide with other stacks that
   developers commonly already run. A hard-coded port makes `make dev` fail on a
   machine that is otherwise fine.

## Decision

- `Settings` resolves the repository-root `.env` by path (`Path(__file__)`
  walked up to the root), then layers a backend-local `.env` on top if present.
  Missing files are ignored, which is the normal case in containers.
- Compose publishes host ports through variables with conventional defaults:
  `POSTGRES_HOST_PORT` (5432), `API_HOST_PORT` (8000), `WEB_HOST_PORT` (5173),
  `MAILPIT_SMTP_HOST_PORT` (1025), `MAILPIT_UI_HOST_PORT` (8025). Only the host
  side is affected; container-to-container addresses stay fixed.
- `DATABASE_URL` in `.env` is the host-facing URL. The `api` service overrides
  it to `postgres:5432` for in-network use.

## Consequences

- Commands behave identically from the repository root or `backend/`.
- A developer with a port conflict edits `.env` instead of the compose file.
- The host-facing `DATABASE_URL` and `POSTGRES_HOST_PORT` must be kept in sync
  by hand; they are adjacent in `.env.example` with a comment saying so.
