# MCP servers for development

`.mcp.json` gives coding agents such as Claude Code three tools for this
repository: the development database, Prometheus, and a browser. They're
development tooling only. Nothing here is part of the product, and nothing
here runs in production. [ADR-0017](adr/0017-development-mcp-servers.md)
explains why each was chosen and how it's limited.

| Server | What an agent can do with it | Runs as / against |
|---|---|---|
| `postgres` | Run read-only SQL, list tables and indexes, inspect active queries and locks | The `grafana_reader` role, which can't read anything users wrote |
| `prometheus` | Run PromQL, list metrics and scrape targets | `http://127.0.0.1:${PROMETHEUS_HOST_PORT}` |
| `playwright` | Open the app, click, type, take snapshots and screenshots, read the console and network | An in-memory Chrome profile that starts logged out |

## Set it up

```bash
make dev-observability   # the stack, plus Prometheus and the grafana_reader role
make mcp-check           # start each server and check it works (other terminal)
```

Then start `claude` in the repository. It asks once whether to trust the
project's MCP servers. Approve them, and `/mcp` shows all three as connected.

Run `make mcp-check` once before you first use them. The first launch
downloads each server, and MCP Toolbox's binary alone takes about a minute.
That's longer than Claude Code waits for a server to start. After that, each
starts in a second or two.

You need `node`/`npx`, `uv`/`uvx`, `git`, and Google Chrome. They're the same
tools the rest of the repository uses, except Chrome, which Playwright MCP
uses by default.

## How it fits together

Each entry in `.mcp.json` runs a launcher in `scripts/mcp/` over **stdio**:
the agent starts the process and talks JSON-RPC over its stdin and stdout. No
server opens a network port.

The launchers exist for one reason. Claude Code expands `${VAR}` in
`.mcp.json` only from its own environment, and it doesn't read `.env`. The
ports and passwords Compose uses live in `.env`, so `scripts/mcp/_env.sh` reads
them from there, without executing the file. The precedence matches Compose:
your shell's environment, then `.env`, then the Compose default. If you
changed `POSTGRES_HOST_PORT`, `PROMETHEUS_HOST_PORT`, `WEB_HOST_PORT`, or
`GRAFANA_DB_READER_PASSWORD`, the servers follow automatically.

Every server version is pinned in its launcher. Dependabot doesn't track
them, so bump them by hand and rerun `make mcp-check`.

## `postgres`: MCP Toolbox for Databases

Google's [MCP Toolbox](https://github.com/googleapis/mcp-toolbox), with its
prebuilt Postgres tools (29 of them, including `execute_sql`, `list_tables`,
`list_active_queries`, `list_locks`, and `get_query_plan`).

**The tool isn't the security boundary; the database role is.** `execute_sql`
runs whatever statement it's given. The launcher logs in as `grafana_reader`
(ADR-0016), and PostgreSQL itself refuses everything that role wasn't granted:

```text
SELECT count(*) FROM runs              -- allowed
SELECT content FROM messages           -- permission denied for table messages
CREATE TABLE x (id int)                -- cannot execute CREATE TABLE in a read-only transaction
```

So an agent can answer "how many runs failed with `run_timeout` this week, by
model?" but not "what did the user ask?". `make mcp-check` asserts the refusal
every time it runs. `tests/integration/test_grafana_reader.py` checks the
grants themselves.

- **Connections:** the role allows 4 connections, which Grafana shares. The
  MCP pool is capped at 2 (`pool_max_conns`), and its sessions show up in
  `pg_stat_activity` as `mcp-postgres`.
- **What it can't see:** if a question needs a column the role can't read,
  the query fails. That's working as intended. Granting another column follows
  the same rule as a new Grafana panel: decide whether the column is private
  first ([observability.md](observability.md#changing-the-dashboard)).
- **Toolbox's startup warnings:** it logs warnings about wildcard origins and
  hosts. These apply to its HTTP mode. Under `--stdio` it opens no port.

## `prometheus`: Prometheus MCP Server

[`prometheus-mcp-server`](https://github.com/pab1it0/prometheus-mcp-server),
run with `uvx`. Its tools are `execute_query`, `execute_range_query`,
`list_metrics`, `get_metric_metadata`, `get_targets`, and `health_check`. It
only ever reads.

Prometheus counters reset when the API restarts, so this answers questions
about the running process, like "what's p95 time-to-first-token right now?"
For anything over days, use the `postgres` server. [observability.md](observability.md)
lists the metrics and what they mean.

## `playwright`: Playwright MCP

Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp). It
works from accessibility snapshots of the page, not screenshots, so its view
of the app is close to what a screen reader announces.

- **Use `http://localhost:5173`**, or whatever your `WEB_HOST_PORT` is, and
  not `127.0.0.1`. The refresh endpoint checks `Origin` against
  `FRONTEND_ORIGIN`, so logging in fails on any other host name.
- **Logged out every session:** `--isolated` keeps the profile in memory, so
  no refresh cookie is left on disk. To test a logged-in flow, register a
  throwaway account. Read verification and reset emails in Mailpit at
  http://localhost:8025.
- **A visible window:** the browser opens in a window you can watch when
  there's a display. Set `PLAYWRIGHT_MCP_HEADLESS=1` to hide it, or
  `PLAYWRIGHT_MCP_BROWSER=firefox` (or `webkit`, `msedge`) to switch browsers.
- **Output files:** snapshots, screenshots, and console logs go to
  `test-results/playwright-mcp/`, which is gitignored.
- **`browser_run_code_unsafe`:** this tool runs arbitrary Playwright code in
  the browser's context. Playwright MCP has no switch to remove it, so leave
  it on "ask" in Claude Code's permissions rather than allowing it outright.

This is for exploring and debugging. The e2e suite in `frontend/tests/e2e/` is
still what proves behaviour (`make e2e`).

## Usage dashboard

Grafana's **MCP usage (development)** dashboard,
<http://localhost:3000/d/mcp-usage>, shows how agents use these servers:
calls, failures, latency, startup time, permission decisions, and the model
cost of reading tool results. [ADR-0018](adr/0018-mcp-usage-dashboard.md)
explains the design.

```text
Claude Code ──OTLP/HTTP──► otel-collector ──/metrics──► Prometheus ──► Grafana
 (events: tool_result,     (127.0.0.1:4318)  (job="mcp")
  tool_decision,           counts events into
  mcp_server_connection;   metrics, keeps only
  cost and token metrics)  allowlisted labels
```

The MCP servers don't report anything themselves. They're short-lived stdio
processes with nothing to scrape. Claude Code, the client, reports every tool
call as an OpenTelemetry event. The collector counts the calls for the three
servers in `.mcp.json` and ignores everything else.

### Turn it on

1. Start the stack with `make dev-observability`, which now includes the
   collector.
2. Give Claude Code these environment variables. A repository can't set
   telemetry for you, so put them in `~/.claude/settings.json` under `"env"`,
   or export them in the shell that starts Claude Code:

   ```bash
   CLAUDE_CODE_ENABLE_TELEMETRY=1
   OTEL_LOGS_EXPORTER=otlp
   OTEL_METRICS_EXPORTER=otlp
   OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
   OTEL_LOG_TOOL_DETAILS=1
   ```

   Change the port if you set `OTLP_HTTP_HOST_PORT`.
3. Start a new Claude Code session. Telemetry is read at startup.
4. `make mcp-check` confirms the collector accepts data. On the dashboard,
   **Telemetry received** rises within a few seconds of your first tool call.

### What's recorded, and what isn't

`OTEL_LOG_TOOL_DETAILS=1` is what puts server and tool names on the events.
It also adds each call's input (the SQL, the URL) and the full error message.
Claude Code sends all of it to the collector on this machine. The collector
keeps only these labels:

| Label | Values |
|---|---|
| `mcp_server`, `mcp_tool` | Names from `.mcp.json` and each server's tool list |
| `success`, `decision`, `status` | `true`/`false`, `accept`/`reject`, `connected`/`failed`/`disconnected` |
| `error_type` | An error class name, such as `McpToolCallError` |
| `error_sqlstate` | A five-character SQLSTATE such as `42501`, cut out of the error message |
| `model`, `type` | Model id; token type (`input`, `output`, `cacheRead`, `cacheCreation`) |

Tool inputs, error messages, prompts, your account and session identifiers,
and the host name never reach Prometheus. The collector's `signal_to_metrics`
connector builds each series only from the attributes it lists.
`tests/unit/test_mcp_dashboard.py` fails if one outside this table is added.

Claude Code reports MCP servers from every project you open. Only `postgres`,
`prometheus`, and `playwright` are counted. A server with one of those names
in another project would be counted too.

### Reading it

- **Right now.** Whether the collector is up and receiving, calls and the
  failure rate in the selected range, how often Postgres refused private
  data (SQLSTATE 42501), and model cost.
- **Calls, Failures, Latency.** Per server and per tool. Refused SQL counts
  as a failure, so a few are expected when an agent probes the read-only role.
- **Servers.** Average startup time (Claude Code gives up at 30 s),
  connection events, and how calls were approved or declined.
- **Model cost.** Claude Code's estimate of model spend on requests it
  attributes to each server, and the tokens behind it. Large tool results,
  like a full page snapshot, cost more because the model reads them.

After editing `observability/otel-collector/config.yaml`, run
`make otel-validate`, then `docker compose --profile observability restart
otel-collector`.

The **MCP server** variable at the top filters every panel. History follows
Prometheus retention (15 days). Counters restart from zero when the collector
restarts, and `increase()` handles that.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/mcp` shows a server as failed right after setup | The first launch was still downloading. Run `make mcp-check`, then reconnect. |
| `password authentication failed for user "grafana_reader"` | The role hasn't been created. Run `make dev-observability`, or `docker compose --profile observability up grafana-db-reader`. |
| `Prometheus has no api target` | Prometheus isn't running (it's in the `observability` profile), or the API is down. |
| `permission denied for table …` | Expected: the column isn't granted to `grafana_reader`. See above. |
| The app's login fails in the MCP browser | The page was opened on `127.0.0.1`. Use `localhost`. |
| The usage dashboard stays empty | Telemetry isn't on in the session you're using (see [Turn it on](#turn-it-on)), or the collector is down. Check **Collector** and **Telemetry received**. |
| **Telemetry received** rises but MCP panels stay empty | `OTEL_LOG_TOOL_DETAILS=1` is missing: without it, events carry no server or tool names. |
