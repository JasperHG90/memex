#!/usr/bin/env bash
# resolve_config.sh — source this file to populate RESOLVED_URL, API, and AUTH_HEADER.
#
# Resolves Memex config (server_url, api_key) via the CLI's `memex config env`
# command, which respects the full config resolution chain:
#   env vars -> local .memex.yaml -> global ~/.config/memex/config.yaml -> defaults
#
# Tries multiple strategies to invoke the CLI, then falls back to
# MEMEX_SERVER_URL env var + no auth.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Portable timeout helper: uses `timeout` (Linux) or `gtimeout` (macOS brew) if available.
_with_timeout() {
    local secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
    else
        "$@"
    fi
}

_try_resolve() {
    local output

    # Strategy 1: memex CLI directly — works if installed via `uv tool install`,
    # `pip install`, or available in the current virtualenv.
    if command -v memex >/dev/null 2>&1; then
        output=$(_with_timeout 5 memex config env 2>/dev/null) && {
            eval "$output"
            return 0
        }
    fi

    # Strategy 2: uvx with package spec from .mcp.json — works for ephemeral uvx
    # usage; reuses the same cached venv as the MCP server.
    if command -v uvx >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
        local pkg_spec
        pkg_spec=$(jq -r '
            .mcpServers.memex.args as $a
            | ($a | index("--from")) as $i
            | if $i != null then $a[$i + 1] else null end
        ' "$PLUGIN_ROOT/.mcp.json" 2>/dev/null) || true

        if [ -n "${pkg_spec:-}" ] && [ "$pkg_spec" != "null" ]; then
            output=$(_with_timeout 10 uvx --quiet --from "$pkg_spec" memex config env 2>/dev/null) && {
                eval "$output"
                return 0
            }
        fi
    fi

    return 1
}

if _try_resolve; then
    RESOLVED_URL="${MEMEX_RESOLVED_URL}"
    if [ -n "${MEMEX_RESOLVED_API_KEY:-}" ]; then
        AUTH_HEADER=(-H "X-API-Key: ${MEMEX_RESOLVED_API_KEY}")
    else
        AUTH_HEADER=()
    fi
else
    # Fallback: env var only, no auth
    RESOLVED_URL="${MEMEX_SERVER_URL:-http://127.0.0.1:8000}"
    AUTH_HEADER=()
fi

API="${RESOLVED_URL}/api/v1"
