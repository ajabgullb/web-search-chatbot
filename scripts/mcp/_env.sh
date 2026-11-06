# Shared by the MCP launchers in this directory (docs/mcp.md). Sourced, not run.
#
# The launchers need the same host ports and passwords Compose uses, and those
# live in the root .env. Claude Code expands ${VAR} in .mcp.json only from its
# own process environment, which does not read .env, so a developer whose
# Postgres is on 5435 would otherwise have to export it by hand.
#
# .env is read, never sourced: sourcing would execute it, and it only needs
# to be data. Precedence matches Compose: the shell environment wins over
# .env, which wins over the default.
#
# Nothing here may write to stdout. A stdio MCP server's stdout is its
# JSON-RPC channel, and one stray line breaks the client's handshake.

MCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# env_value KEY DEFAULT
env_value() {
    local key="$1" default="$2" line value
    if [[ -n "${!key:-}" ]]; then
        printf '%s' "${!key}"
        return
    fi
    line="$(grep -E "^[[:space:]]*${key}=" "$MCP_ROOT/.env" 2>/dev/null | tail -n 1 || true)"
    value="${line#*=}"
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
    else
        # An unquoted value ends at " #", as Compose reads it.
        value="${value%%[[:space:]]#*}"
        value="${value%"${value##*[![:space:]]}"}"
    fi
    printf '%s' "${value:-$default}"
}
