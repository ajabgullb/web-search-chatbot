# Observability

What the app records, where to look at it, and how to change the dashboard.
The design decisions are in [ADR-0016](adr/0016-operator-dashboard.md).

## Start it

```bash
make dev-observability     # the normal stack plus Prometheus and Grafana
```

| What | Where |
| --- | --- |
| Dashboard (Grafana) | <http://localhost:3000>. Opens on the dashboard without login. Sign in as `admin` / `admin` to edit. |
| Prometheus | <http://localhost:9090>. Raw queries; *Status → Targets* shows whether the API is being scraped. |
| Raw metrics | <http://localhost:8000/api/v1/metrics> (your `API_HOST_PORT`). This is what Prometheus reads. |

Ports are overridable with `GRAFANA_HOST_PORT` and `PROMETHEUS_HOST_PORT`, and
bind to `127.0.0.1` only. `make down` stops these services too.

How the pieces fit: the API counts events in memory, and Prometheus fetches
those counts every 15 s and stores the history. Grafana asks Prometheus for
rates and percentiles over time, and asks PostgreSQL for daily totals.

## Reading the dashboard

Rows run from most to least urgent. Hover a panel's ⓘ for what it means and
when to worry.

1. **Right now.** API up, active streams, run failure rate, 5xx rate,
   p95 time to first token, database pool in use. Tiles turn amber or red at
   thresholds.
2. **Traffic and errors.** Requests by status class, p95 latency per route,
   and 5xx responses per route.
3. **Runs.** Outcomes, failures by error code, time to first token, run
   duration against the 90 s deadline, and searches per run.
4. **Providers.** Failures by provider, search outcomes and latency against
   the 20 s timeout, embedding latency. This is where you tell "the search
   provider is down" from "we are broken".
5. **Cost, live.** Tokens per minute by model, tokens per run, and embedding
   tokens.
6. **Answer quality.** Share of unverified (degraded) answers, what answers
   drew on (direct / web / documents), and document-search outcomes.
7. **Documents.** Uploads by outcome, ingestion results, processing time,
   and documents stuck in `processing`.
8. **Abuse and auth.** Rate-limit refusals, and 401/429 responses on `/auth`.
9. **Usage and spend (database).** Active users, sign-ups, runs, estimated
   spend, and the top failure codes. These panels survive API restarts.

Interaction: drag across any chart to zoom, use the time picker (top right)
for the range, click a legend entry to isolate a series, and press `v` on a
panel to view it full-screen. The **price variables** at the top drive the
spend panels. Update them when prices change or you switch `LLM_MODEL`.

**Live panels reset when the API restarts.** Prometheus counters live in the
process, so after a restart (including `--reload` in development) the live
rows start again from zero. The database row does not reset.

## Changing the dashboard

The committed JSON is the source of truth, and Grafana will not overwrite it
from the UI.

1. Edit the panel in Grafana as `admin`.
2. **Export → Export as JSON**, and save it over
   `observability/grafana/dashboards/web-search-chatbot.json`.
3. Run `make test-backend`. `tests/unit/test_dashboard.py` checks that every
   metric a panel queries exists and is scoped to `job="api"`.
   `tests/integration/test_grafana_reader.py` runs every SQL panel as the
   read-only role.

Grafana reloads the file within 30 s.

**A panel that needs a new database column** fails with `permission denied`
until the column is added to `observability/postgres/grafana_reader.sql`.
That's deliberate: think about whether the column is private first. Recreate
the role with `docker compose --profile observability up grafana-db-reader`.

**A new metric** goes in `backend/app/core/telemetry.py`. Label values must
come from a small fixed set: never an id, an email or user text. Each
distinct value is a new time series stored forever.

## Production (not yet provisioned)

The production stack does not run Prometheus or Grafana yet. To add them:

- Set `METRICS_TOKEN` (32+ characters). Prometheus then sends it via
  `authorization: { credentials_file: ... }` in its scrape config. Without the
  token, production returns `404` for `/metrics`.
- List or discover every API replica as a scrape target: each one counts only
  its own work.
- Put Grafana behind real authentication (turn anonymous access off), and
  reach it over a VPN or SSH tunnel rather than a public port.
- Give `grafana_reader` its own strong password via
  `GRAFANA_DB_READER_PASSWORD`.
