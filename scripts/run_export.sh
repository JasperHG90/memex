#!/usr/bin/env bash
# One-shot wrapper: source .env for DB password, run export
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env if MEMEX_DB_PASSWORD not already set
if [[ -z "${MEMEX_DB_PASSWORD:-}" ]]; then
  if [[ -f .env ]]; then
    set -a
    source .env
    set +a
  else
    echo "ERROR: MEMEX_DB_PASSWORD not set and no .env found." >&2
    echo "Run: vault.ps1 distribute -M homelab" >&2
    exit 1
  fi
fi

uv run python scripts/memex_export_local.py "$@"
