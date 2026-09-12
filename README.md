# Web Search Chatbot

A full-stack chatbot that answers questions with an LLM, searches the public web
when that helps, streams its answer, keeps conversation history, and cites its
sources.

- [`AGENTS.md`](./AGENTS.md) — the implementation contract.
- [`PLAN.md`](./PLAN.md) — the nine-phase roadmap and per-phase definition of done.
- [`docs/`](./docs) — architecture, API, security, and decision records.

**Current status: Phase 5 (agent core).** You can register, sign in,
stay signed in across reloads, reset a password from an emailed link, and sign
out. Inside the app you can create, rename, delete, and page through your own
conversations, and open one to read its history rendered as sanitized Markdown
with highlighted code.

Behind the scenes the LangGraph agent now exists: it decides whether to search,
runs bounded Tavily searches, answers with validated citations, and checkpoints
each conversation thread to PostgreSQL. It is proven entirely through tests
with fake providers. The composer still **does not reach the model** — the run
endpoint and SSE stream that connect the two arrive in Phases 6–7.

No provider key is needed to run the app or the tests in development. Set
`LLM_API_KEY` and `SEARCH_API_KEY` in `.env` once Phase 6 makes the agent
reachable; production refuses to start without them.

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
make dev                  # builds and starts api, web, postgres, mailpit
```

| Service | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API | http://localhost:8000/api/v1 |
| API docs | http://localhost:8000/api/v1/docs |
| Mailpit | http://localhost:8025 |

If another project already uses one of those ports, change `POSTGRES_HOST_PORT`,
`API_HOST_PORT`, `WEB_HOST_PORT`, `MAILPIT_SMTP_HOST_PORT`, or
`MAILPIT_UI_HOST_PORT` in `.env` — and keep the port in `DATABASE_URL` in step
with `POSTGRES_HOST_PORT`, since host tools connect through it.

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

## Layout

```
backend/    FastAPI app (app/), Alembic migrations, tests
frontend/   Vite + React app (src/), Playwright e2e (tests/e2e)
docs/       architecture, API, security, ADRs
compose.yaml  local development stack
Makefile      task runner
openapi.json  committed API schema; the frontend client is generated from it
```

## Conventions

- Every schema change ships an Alembic migration **and** a working downgrade.
- Route handlers stay thin: validate, call a service, map the result. SQL lives
  in repositories, orchestration in services, agent behaviour in `app/agent`.
- `app/core/config.py` is the only place that reads the environment.
- Auth tokens never touch `localStorage` — ESLint enforces it.
- `Settings` is resolved from `app.state`, never from the cached global, so an
  app is always governed by the settings it was constructed with.
- Generated API client files are never edited by hand; change the backend
  schema and regenerate.
