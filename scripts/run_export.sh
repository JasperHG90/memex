#!/usr/bin/env bash
# One-shot wrapper: source .env for DB password, run export
set -euo pipefail
cd "$(dirname "$0")/.."

# Parse .env safely if MEMEX_DB_PASSWORD not already set
if [[ -z "${MEMEX_DB_PASSWORD:-}" ]]; then
  if [[ -f .env ]]; then
    while IFS='=' read -r key value; do
      [[ -z "$key" || "$key" == \#* ]] && continue
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      value="${value%\"}" ; value="${value#\"}"
      value="${value%\'}" ; value="${value#\'}"
      export "$key=$value"
    done < .env
  else
    echo "ERROR: MEMEX_DB_PASSWORD not set and no .env found." >&2
    echo "Run: vault.ps1 distribute -M homelab" >&2
    exit 1
  fi
fi

uv run python scripts/memex_export_local.py "$@"
