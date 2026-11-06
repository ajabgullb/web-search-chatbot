#!/usr/bin/env bash
# MCP server: the development database, as the read-only grafana_reader role.
# See docs/mcp.md and ADR-0017.
#
# The server is Google's MCP Toolbox for Databases with its prebuilt Postgres
# tools. Its execute_sql tool runs any SQL it is given, so what it can see is
# decided entirely by the role it logs in as, never by the tool. That is why
# this logs in as grafana_reader (ADR-0016): column-level SELECT grants, a
# read-only default transaction, and a 5 s statement timeout. It can count
# messages, runs and documents, but PostgreSQL refuses it message content,
# emails, password hashes, sessions, titles, filenames and document chunks.
#
# The role exists once `make dev-observability` has run its one-shot
# grafana-db-reader service.
set -euo pipefail
# shellcheck source=scripts/mcp/_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT="$(env_value POSTGRES_HOST_PORT 5432)"
export POSTGRES_DATABASE="$(env_value POSTGRES_DB app)"
# Set on purpose, not read from .env: POSTGRES_USER there is the app's owner
# role, which can read everything, including every user's messages.
export POSTGRES_USER=grafana_reader
export POSTGRES_PASSWORD="$(env_value GRAFANA_DB_READER_PASSWORD grafana_reader)"
# Toolbox pastes this into YAML, so it must be a YAML mapping. The role has
# a 4-connection limit that Grafana shares, so the MCP pool takes at most 2.
# The application name labels these sessions in pg_stat_activity.
export POSTGRES_QUERY_PARAMS='{pool_max_conns: "2", application_name: "mcp-postgres"}'

# Claude Code passes its own OTEL_* telemetry settings down to this process,
# and Toolbox would then export its metrics too. They can't be used: every
# Toolbox process reports cumulative series with the same identity, so two
# sessions overwrite each other's counts. The usage dashboard counts calls
# from Claude Code's events instead (ADR-0018).
unset "${!OTEL_@}"

# --stdio opens no network port. The wildcard-origin warnings Toolbox logs at
# startup apply only to its HTTP mode.
exec npx -y @toolbox-sdk/server@1.16.0 --prebuilt=postgres --stdio
