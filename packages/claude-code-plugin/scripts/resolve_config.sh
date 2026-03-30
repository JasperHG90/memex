#!/usr/bin/env bash
# resolve_config.sh — defines the memex() shell function via uvx.
#
# Source this from hook scripts. All CLI config resolution (server URL,
# API key, vault selection) is handled internally by the memex CLI.

if ! command -v uvx >/dev/null 2>&1; then
    cat <<'EOF'
{"systemMessage": "❌ `uvx` is not on PATH. Hooks require it to run the Memex CLI.\n\nInstall uv: https://docs.astral.sh/uv/getting-started/installation/"}
EOF
    exit 0
fi

memex() { uvx --from memex-cli memex "$@"; }
