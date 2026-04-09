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

_memex_ref="${MEMEX_PLUGIN_VERSION:-latest}"
_memex_pkg="memex-cli @ git+https://github.com/JasperHG90/memex.git@${_memex_ref}#subdirectory=packages/cli"

# Validate ref exists when user overrides the default
if [ "$_memex_ref" != "latest" ]; then
    if ! git ls-remote --tags --heads https://github.com/JasperHG90/memex.git "$_memex_ref" 2>/dev/null | grep -q .; then
        cat <<EOF
{"systemMessage": "❌ MEMEX_PLUGIN_VERSION='${_memex_ref}' does not exist as a tag or branch on github.com/JasperHG90/memex.\n\nAvailable tags: https://github.com/JasperHG90/memex/tags\n\nUnset the variable to use the default (latest)."}
EOF
        exit 0
    fi
fi

memex() { uvx --from "$_memex_pkg" memex "$@"; }
