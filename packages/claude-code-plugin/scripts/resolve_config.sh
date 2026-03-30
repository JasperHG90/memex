#!/usr/bin/env bash
# resolve_config.sh — source this to populate RESOLVED_URL, API, and AUTH_HEADER.
#
# Resolves Memex config via `memex config env` (respects the full config chain).
# Falls back to MEMEX_SERVER_URL env var + no auth if the CLI isn't available.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v memex >/dev/null 2>&1; then
    eval "$(memex config env 2>/dev/null)" || true
fi

RESOLVED_URL="${MEMEX_RESOLVED_URL:-${MEMEX_SERVER_URL:-http://127.0.0.1:8000}}"
if [ -n "${MEMEX_RESOLVED_API_KEY:-}" ]; then
    AUTH_HEADER=(-H "X-API-Key: ${MEMEX_RESOLVED_API_KEY}")
else
    AUTH_HEADER=()
fi

API="${RESOLVED_URL}/api/v1"
