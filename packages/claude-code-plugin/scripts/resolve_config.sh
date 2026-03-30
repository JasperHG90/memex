#!/usr/bin/env bash
# resolve_config.sh — source this to populate RESOLVED_URL, API, and AUTH_HEADER.
#
# Resolves Memex config via `memex config env` (respects the full config chain).
# Fails loudly if `memex` is not on PATH.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v memex >/dev/null 2>&1; then
    cat <<'EOF'
{"systemMessage": "❌ `memex` CLI is not on PATH. Hooks require it for config resolution.\n\nFix: run `uv tool install memex-cli` to make `memex` available system-wide."}
EOF
    exit 0
fi

eval "$(memex config env 2>/dev/null)" || {
    cat <<'EOF'
{"systemMessage": "❌ `memex config env` failed. Check your Memex configuration."}
EOF
    exit 0
}

RESOLVED_URL="${MEMEX_RESOLVED_URL}"
if [ -n "${MEMEX_RESOLVED_API_KEY:-}" ]; then
    AUTH_HEADER=(-H "X-API-Key: ${MEMEX_RESOLVED_API_KEY}")
else
    AUTH_HEADER=()
fi

API="${RESOLVED_URL}/api/v1"
