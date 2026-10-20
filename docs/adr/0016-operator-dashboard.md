# 16. Operator dashboard: Prometheus and Grafana, provisioned from the repo

Date: 2026-10-20

## Status

Accepted.

## Context

The API has exported Prometheus metrics since Phase 8, but nothing stored or
drew them. `/metrics` shows only current counter values, as plain text: there's
no history, rates or percentiles, and no way to tell whether 3 failures is a lot.

AGENTS.md §2 lists "admin dashboard" as a non-goal. We read that as an in-app
admin UI: pages, roles, user management. An operator view of metrics the app
already exports is observability (§12), not product scope.

Three gaps stood in the way of the panels that matter most:

- Failed runs were counted, but not *why* they failed.
- Nothing measured answer quality. A degraded answer (searched, but no valid
  citation) was saved on the message and never counted.
- Tokens had no model label, so cost could not be priced per model.

## Decision

- **Prometheus and Grafana as an optional Compose profile**
  (`make dev-observability`), not part of `make dev`. They are pinned images
  with no new application dependency, and their ports bind to `127.0.0.1`.
- **Provisioned from files.** The datasources and the dashboard are committed
  under `observability/`. The dashboard JSON is the source of truth. Grafana
  will not save UI edits over it; you export and commit them instead.
- **Two sources, by what each is good at.** Prometheus covers live operations:
  rates, percentiles and errors. Its counters reset on every API restart and
  deliberately carry no per-user labels, so durable daily history (active
  users, runs per day, estimated spend) comes from PostgreSQL.
- **A column-limited, read-only database role.** `grafana_reader` gets
  `SELECT` on named columns only, plus `default_transaction_read_only`, a 5 s
  statement timeout and a 4-connection limit. It can count messages but
  cannot read their content, an email, a password hash, a session, a title, a
  filename or a document chunk. PostgreSQL enforces this, so a careless panel
  query fails instead of leaking.
- **Three new series:** `run_failures_total{code}`,
  `answers_total{source,degraded}`, and a `model` label on `llm_tokens_total`.
  Every label value comes from a small fixed set, as the other metrics' do.
- **Spend is an estimate driven by dashboard variables:** price per million
  tokens and per search, defaulting to gpt-4o-mini, text-embedding-3-small
  and Tavily list prices. They are interpolated with `:sqlstring`, so a
  viewer's input is quoted, not executed.

## Consequences

- `tests/unit/test_dashboard.py` fails if a panel queries a metric
  `telemetry.py` no longer defines. Otherwise a rename shows up only as a
  "No data" panel, which looks like "nothing happened".
- `tests/integration/test_grafana_reader.py` runs every SQL panel as
  `grafana_reader` and checks that private columns are refused. Adding a
  panel that needs a new column means adding a grant here, on purpose.
- Live panels show only what happened since the API process last started.
  That's how Prometheus counters behave, and why the durable panels exist.
- Development only, for now. Production needs Prometheus to present
  `METRICS_TOKEN`, Grafana behind real authentication, and one scrape target
  per replica. `docs/observability.md` sketches it. Alerting rules are not
  part of this decision.
