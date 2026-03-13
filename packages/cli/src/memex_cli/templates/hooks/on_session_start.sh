#!/usr/bin/env bash
# Memex Claude Code Hook — SessionStart
# Loads recent memories and vault context at session start.
set -euo pipefail

# Guard: uv must be on PATH
command -v uv >/dev/null 2>&1 || exit 0

# --- Vault context from local .memex.yaml ---
for cfg in .memex.yaml memex_core.yaml memex_core.config.yaml; do
    if [ -f "$cfg" ]; then
        active=$(grep -oP '^\s*active:\s*\K\S+' "$cfg" 2>/dev/null || true)
        search=$(grep -A 20 'search:' "$cfg" 2>/dev/null | grep -oP '^\s*-\s*\K\S+' || true)
        if [ -n "$active" ] || [ -n "$search" ]; then
            echo "## Memex Vault Context"
            echo ""
            [ -n "$active" ] && echo "- **Writer vault**: $active"
            if [ -n "$search" ]; then
                echo "- **Search vaults**: $(echo "$search" | tr '\n' ', ' | sed 's/,$//')"
            fi
            echo ""
        fi
        break
    fi
done

# --- KV preferences/facts ---
kv_output=$(uv run memex kv list --json 2>/dev/null) || true

if [ -n "$kv_output" ] && [ "$kv_output" != "[]" ]; then
    echo "## Memex KV Facts (user preferences & conventions)"
    echo ""
    echo "$kv_output"
    echo ""
fi

# --- Recent notes with IDs ---
output=$(uv run memex note recent --limit 5 --compact 2>/dev/null) || exit 0

[ -z "$output" ] && exit 0

echo "## Recent Memex Notes"
echo ""
echo "$output"
