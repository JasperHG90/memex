#!/usr/bin/env bash
# Memex Claude Code Hook — SessionStart
# Loads recent memories and vault context at session start.
set -euo pipefail

# Guard: uv must be on PATH
command -v uv >/dev/null 2>&1 || exit 0

# --- Vault context from local .memex.yaml ---
for cfg in .memex.yaml memex_core.yaml memex_core.config.yaml; do
    if [ -f "$cfg" ]; then
        active=$(grep -oP '^\s*active_vault:\s*\K\S+' "$cfg" 2>/dev/null || true)
        attached=$(grep -A 20 'attached_vaults:' "$cfg" 2>/dev/null | grep -oP '^\s*-\s*\K\S+' || true)
        if [ -n "$active" ] || [ -n "$attached" ]; then
            echo "## Memex Vault Context"
            echo ""
            [ -n "$active" ] && echo "- **Writer vault**: $active"
            if [ -n "$attached" ]; then
                echo "- **Attached vaults** (read-only): $(echo "$attached" | tr '\n' ', ' | sed 's/,$//')"
            fi
            echo ""
        fi
        break
    fi
done

# --- Recent notes with IDs ---
output=$(uv run memex note recent --limit 5 --compact 2>/dev/null) || exit 0

[ -z "$output" ] && exit 0

echo "## Recent Memex Notes"
echo ""
echo "$output"
