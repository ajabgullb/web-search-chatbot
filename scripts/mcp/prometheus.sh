#!/usr/bin/env bash
# MCP server: the development Prometheus. See docs/mcp.md and ADR-0017.
#
# Read-only by construction: the server only calls Prometheus's query and
# metadata HTTP endpoints. Our metrics carry no per-user labels (ADR-0016),
# so nothing it returns identifies a user.
#
# Prometheus runs under `make dev-observability`. Its series reset whenever
# the API restarts, so this sees the live process, not history. Durable
# history is in PostgreSQL, reached through the postgres MCP server.
set -euo pipefail
# shellcheck source=scripts/mcp/_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

export PROMETHEUS_URL="http://127.0.0.1:$(env_value PROMETHEUS_HOST_PORT 9090)"

exec uvx prometheus-mcp-server@1.6.2
