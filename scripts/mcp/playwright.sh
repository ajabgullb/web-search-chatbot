#!/usr/bin/env bash
# MCP server: a browser an agent can drive, for exploring and debugging the
# frontend. See docs/mcp.md and ADR-0017.
#
# --isolated keeps the browser profile in memory. Each session starts logged
# out and leaves no refresh cookie on disk, the same as the e2e suite.
#
# The browser opens a visible window when there is a display to show it on,
# so you can watch what the agent does. Set PLAYWRIGHT_MCP_HEADLESS=1 to hide
# it. The default browser is the installed Google Chrome; set
# PLAYWRIGHT_MCP_BROWSER (chrome, firefox, webkit, msedge) to change it.
#
# Open the app at http://localhost:<WEB_HOST_PORT>, not 127.0.0.1: the refresh
# endpoint checks Origin against FRONTEND_ORIGIN, so login breaks on any
# other host name.
set -euo pipefail
# shellcheck source=scripts/mcp/_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

# Screenshots, page snapshots and console logs. Gitignored (test-results/).
args=(--isolated --output-dir "$MCP_ROOT/test-results/playwright-mcp")

if [[ "$(env_value PLAYWRIGHT_MCP_HEADLESS 0)" == 1 || -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    args+=(--headless)
fi
browser="$(env_value PLAYWRIGHT_MCP_BROWSER "")"
if [[ -n "$browser" ]]; then
    args+=(--browser "$browser")
fi

exec npx -y @playwright/mcp@0.0.84 "${args[@]}"
