#!/usr/bin/env bash
# Memex Claude Code Hook — SessionStart
# Loads recent memories and vault context at session start.
set -euo pipefail

# Guard: uv must be on PATH
command -v uv >/dev/null 2>&1 || exit 0

# --- Vault table with note counts ---
vault_output=$(uv run memex vault list --compact 2>/dev/null) || true
if [ -n "$vault_output" ]; then
    echo "## Memex Vaults"
    echo ""
    echo "$vault_output"
    echo ""
fi

# --- KV preferences/facts ---
kv_output=$(uv run memex kv list --json 2>/dev/null) || true

if [ -n "$kv_output" ] && [ "$kv_output" != "[]" ]; then
    echo "## Memex KV Facts (user preferences & conventions)"
    echo ""
    echo "$kv_output"
    echo ""
fi

# --- Recent notes with IDs ---
output=$(uv run memex note recent --limit 10 --compact 2>/dev/null) || exit 0

[ -z "$output" ] && exit 0

echo "## Recent Memex Notes"
echo ""
echo "$output"
