# 18. MCP usage dashboard from Claude Code's telemetry

Date: 2026-11-06

## Status

Accepted.

## Context

ADR-0017 gave coding agents three MCP servers. The product owner asked for a
dashboard of how they're used: calls, failures, latency, startup time, and
cost.

Nothing in our stack measured that. The servers are short-lived stdio
processes that Claude Code starts and stops each session, so Prometheus has
nothing to scrape. Pushing data out was the only option, and we checked what
each part can push by capturing its telemetry:

- **Claude Code** emits an OpenTelemetry `tool_result` event for every tool
  call, with success and duration, plus `tool_decision` and
  `mcp_server_connection` events. Its cost and token metrics carry an
  `mcp_server.name` label. Server and tool names appear only with
  `OTEL_LOG_TOOL_DETAILS=1`. That setting also adds the call's input (the SQL
  text, the URL) and the full error message. Every event also carries
  account and session identifiers.
- **MCP Toolbox** exports its own metrics. But every Toolbox process reports
  cumulative series with the same identity and no instance id. It ignores
  both `OTEL_RESOURCE_ATTRIBUTES` and the temporality setting, so two sessions
  would overwrite each other's counts.
- **Prometheus MCP and Playwright MCP** report nothing.

## Decision

- **One source: Claude Code's telemetry.** It covers all three servers the
  same way. Toolbox's exporter is switched off in its launcher by unsetting
  the inherited `OTEL_*` variables.
- **An OpenTelemetry Collector** (`otel/opentelemetry-collector-contrib`,
  pinned) in the `observability` profile. It receives OTLP/HTTP on
  `127.0.0.1:4318` only. Its `signal_to_metrics` connector counts events into
  metrics, and Prometheus scrapes them as `job="mcp"`. Grafana provisions a
  second dashboard, `mcp-usage`.
- **Privacy by construction.** The connector builds each series from an
  explicit attribute list, so anything unlisted is dropped by default. That
  covers tool inputs, error messages, prompts, and account, session and host
  identifiers. SQL errors are reduced to their SQLSTATE code. A unit test
  pins the allowlist and the server allowlist, which must match `.mcp.json`.
- **Only this project's servers** (`postgres`, `prometheus`, `playwright`) are
  counted. Claude Code reports servers from every project, and those names
  would otherwise grow the series count without limit.
- **Counter correctness.** Prometheus runs with
  `created-timestamp-zero-ingestion`, and the collector gives every series a
  start time. Without both, a counter's first increment is invisible. MCP
  counters are low-volume, so "used once" would read as "never used".
- **Averages for startup time.** Each session starts each server once. That
  is too few samples for `histogram_quantile`, which would only interpolate
  inside one bucket.

## Consequences

- The dashboard needs telemetry turned on where Claude Code runs. A
  repository can't do that, so it's a per-developer setting, documented in
  docs/mcp.md. Sessions without it are simply not counted.
- Only Claude Code's usage is measured. Another MCP client would need its own
  telemetry.
- `OTEL_LOG_TOOL_DETAILS=1` sends tool inputs to the local collector. They
  stay on this machine and are discarded before storage, but they are sent.
- The Prometheus feature flag applies to every scrape job, but it changes
  nothing for the API today. `/api/v1/metrics` serves the Prometheus text
  format, where `_created` is an ordinary gauge, not a start time. The API's
  counters keep the first-increment gap. Serving OpenMetrics there would
  close it, and that is a separate change.
- The connector and `delta_to_cumulative` are alpha-stability components. The
  versions are pinned and `make otel-validate` checks the config with the
  pinned binary, but a collector upgrade must be re-verified against the
  dashboard.
