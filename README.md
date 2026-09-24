# Web Search Chatbot

A full-stack chatbot that answers questions with an LLM, searches the public web
when that helps, streams its answer, keeps conversation history, and cites its
sources.

- [`AGENTS.md`](./AGENTS.md) — the implementation contract.
- [`PLAN.md`](./PLAN.md) — the nine-phase roadmap and per-phase definition of done.
- [`docs/`](./docs) — architecture, API, security, and decision records.

**Current status: Phase 7 (streaming frontend).** You can register, sign in,
stay signed in across reloads, reset a password, and manage your conversations.
Asking a question now streams the answer into the thread as it is written —
with live search progress, inline citation buttons, and source cards — then
replaces it with the saved message. You can stop an answer (the server stops
too), retry a failed or stopped one without duplicating the question, and
reload mid-answer: the page reconnects and shows the result. See
[`docs/architecture.md`](docs/architecture.md#streaming-frontend).

No provider key is needed to run the app or the tests. To get real answers, set
`LLM_API_KEY` (and `LLM_BASE_URL`/`LLM_MODEL` for a non-OpenAI endpoint such as
Ollama) and `SEARCH_API_KEY` in `.env`; production refuses to start without
them.

## Stack

| | |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, PostgreSQL 16 |
| Agent | LangChain + LangGraph (explicit graph, PostgreSQL checkpointer), Tavily search |
| Frontend | React 19, TypeScript (strict), Vite, React Router, TanStack Query, Tailwind CSS v4, shadcn/ui |
| Tooling | uv, npm, Ruff, mypy, ESLint, Prettier, pytest, Vitest, Playwright |
| Runtime | Docker Compose; GitHub Actions for CI |

## Quick start

Requires Docker with Compose v2+. For host-side commands you also need
[uv](https://docs.astral.sh/uv/) and Node 24.

```bash
cp .env.example .env      # then edit if any default port is taken
make dev                  # builds and starts api, web, postgres, redis, mailpit
```

| Service | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API | http://localhost:8000/api/v1 |
| API docs | http://localhost:8000/api/v1/docs |
| Mailpit | http://localhost:8025 |

If another project already uses one of those ports, change `POSTGRES_HOST_PORT`,
`API_HOST_PORT`, `WEB_HOST_PORT`, `REDIS_HOST_PORT`, `MAILPIT_SMTP_HOST_PORT`, or
`MAILPIT_UI_HOST_PORT` in `.env`. Keep the port in `DATABASE_URL` in step with
`POSTGRES_HOST_PORT`, and the one in `REDIS_URL` in step with `REDIS_HOST_PORT`,
since host tools connect through them.

Verify the API is up:

```bash
curl -s localhost:8000/api/v1/health/live    # {"status":"ok"}
curl -s localhost:8000/api/v1/health/ready   # {"status":"ready","checks":{"database":true}}
```

## Working on it

Backend tasks run on the host through `uv`, which is faster than round-tripping
through a container. They need PostgreSQL, so start it first:

```bash
make up-db        # PostgreSQL only
make install      # uv sync --frozen + npm ci
make migrate      # alembic upgrade head
```

After adding a backend or frontend dependency, run `make rebuild` — the dev
containers cache their `.venv` and `node_modules` in anonymous volumes that a
plain rebuild will not refresh.

`migrate`, `migration`, `downgrade`, and `test-backend` run a `check-db`
preflight first, so a stopped database gives you one clear line instead of a
driver traceback. Run `make check-db` on its own to see where `DATABASE_URL`
actually points.

Integration tests never touch your development data: they create and migrate
a sibling database named `<your database>_test` on the same server (`app_test`
by default) and truncate only that. Set `TEST_DATABASE_URL` to use another.

```bash
make help         # list every target
make test         # pytest + vitest
make lint         # ruff + eslint + prettier --check
make typecheck    # mypy + tsc
make format       # auto-fix both sides
make openapi          # regenerate ./openapi.json
make generate-client  # regenerate ./openapi.json and the typed frontend client
make down         # stop the stack
```

## Running in production

`compose.prod.yaml` runs the images that ship, with production settings:

```bash
cp .env.production.example .env.production   # fill in every value
make prod-up                                 # build, migrate, start, wait until healthy
make prod-logs
make prod-down                               # data volumes are kept
```

- **One public port.** `web` (nginx) serves the SPA and proxies `/api` to the
  API, which publishes no port. Terminate TLS in front of the stack (a load
  balancer, Caddy, or Traefik) and forward to `WEB_HOST_PORT` with
  `X-Forwarded-Proto: https`. nginx then adds HSTS.
- **Migrations run once**, as the `migrate` job. The API starts only after it
  has exited successfully, and replicas never race to migrate.
- **The configuration fails closed at two levels.** Compose refuses to start
  and names any missing required value. The API then refuses to boot on weak
  or unsafe settings: see `docs/security.md`. Always pass
  `--env-file .env.production` (the `make` targets do). Without it, Compose
  falls back to the development `.env`.
- **Stopping drains.** The API has a 100 s graceful timeout and a 120 s stop
  grace period, so answers in progress finish. New runs get `503` until the
  process exits.
- **Observability.** JSON logs go to stdout. Prometheus scrapes
  `/api/v1/metrics` with `Authorization: Bearer $METRICS_TOKEN`; without a
  token the endpoint does not exist. LangSmith tracing is optional and never
  uploads content.

Both images run as non-root users, carry health checks, and contain no
secrets. Everything sensitive arrives through the environment at run time.

## Layout

```
backend/    FastAPI app (app/), Alembic migrations, tests
frontend/   Vite + React app (src/), Playwright e2e (tests/e2e)
docs/       architecture, API, security, ADRs
compose.yaml  local development stack
compose.prod.yaml  production-shaped stack (see "Running in production")
Makefile      task runner
openapi.json  committed API schema; the frontend client is generated from it
```

## Conventions

- Every schema change ships an Alembic migration **and** a working downgrade.
- Route handlers stay thin: validate, call a service, map the result. SQL lives
  in repositories, orchestration in services, agent behaviour in `app/agent`.
- `app/core/config.py` is the only place that reads the environment. (The one
  write is `app/agent/tracing.py` switching off LangChain's implicit tracing.)
- Log with an event name and explicit fields, never interpolated values:
  `logger.info("run_accepted", extra={"retry": False})`. Never log content.
- Auth tokens never touch `localStorage` — ESLint enforces it.
- `Settings` is resolved from `app.state`, never from the cached global, so an
  app is always governed by the settings it was constructed with.
- Generated API client files are never edited by hand; change the backend
  schema and regenerate.
