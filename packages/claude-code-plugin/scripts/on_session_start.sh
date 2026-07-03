#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Auto-installs/updates rules file.
# 2. Fetches token-budgeted session briefing via single CLI call.
# 3. Resolves active vault from the hierarchical resolver (project → user → agent → env → default).
#
# Dependencies: uvx (uv), jq, git (optional)
set -uo pipefail
# Match every other hook: any unguarded failure emits `{}` so Claude Code
# always sees valid hookSpecificOutput JSON for SessionStart (rather than
# `set -e` quietly killing the script mid-write with no payload). The
# EXIT trap registered later for `tmp_briefing` cleanup composes cleanly
# with this one because bash invokes both.
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# SessionStart's only output is JSON; let the resolver surface uvx/version
# errors as systemMessage so the user sees them.
MEMEX_RESOLVE_VERBOSE=1 source "$SCRIPT_DIR/resolve_config.sh"

# resolve_config.sh sets MEMEX_HOOK_ALREADY_EMITTED=1 if it already wrote
# a user-actionable JSON document to stdout (e.g. uvx is missing). In
# that case, the SessionStart contract requires exactly one JSON document
# — exit cleanly rather than emit a second one downstream.
if [ "${MEMEX_HOOK_ALREADY_EMITTED:-0}" = "1" ]; then
    exit 0
fi

# Read the SessionStart payload from stdin. Empty stdin is OK (defensive
# for invocations outside Claude Code, e.g. unit tests).
_payload=""
if [ ! -t 0 ]; then
    _payload=$(cat)
fi

# --- Dependency check: jq ---
if ! command -v jq >/dev/null 2>&1; then
    cat <<'NOJQ'
{"systemMessage": "jq is not installed. Memex hooks require jq for reliable JSON handling.\n\nInstall it: apt-get install jq (Debian/Ubuntu), brew install jq (macOS), or see https://jqlang.github.io/jq/download/\n\nMemex MCP tools still work, but hook context injection is degraded."}
NOJQ
    exit 0
fi

# --- Resolve project root (used by the agent-surface rule install below) ---
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    _project_root=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
else
    _project_root="$PWD"
fi

# --- Clear stale session state ---
# Per-session files written by other hooks (PreCompact, SessionEnd, PreToolUse
# trackers) carry the previous session's id in their suffix. Wipe them at
# SessionStart so they can't leak into the new session — otherwise we keep
# growing files in $STATE_DIR for the lifetime of the plugin install.
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR/write_count"
rm -rf "$STATE_DIR/file_edits"
rm -f "$STATE_DIR"/capture_count_*
rm -f "$STATE_DIR"/session_note_offset_*
rm -f "$STATE_DIR"/session_note_created_*
# Also clear the cached CC session id; it is rewritten below from the
# current payload. A stale id would cause `/handoff` notes to anchor to
# the wrong session.
rm -f "$STATE_DIR/cc_session_id"

# --- Generate session note key ---
SESSION_NOTE_KEY="session:$(date -u +%Y-%m-%dT%H:%M:%S.%3N 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)"
echo "$SESSION_NOTE_KEY" > "$STATE_DIR/session_note_key"

# --- Cache identifiers from the SessionStart payload for later hooks ---
# `model` is only present on the SessionStart payload — other events don't
# carry it — so PreToolUse hooks need it cached on disk.
if [ -n "$_payload" ]; then
    _model=$(printf '%s' "$_payload" | jq -r '.model // empty' 2>/dev/null || true)
    [ -n "$_model" ] && echo "$_model" > "$STATE_DIR/model"

    _cc_session_id=$(printf '%s' "$_payload" | jq -r '.session_id // empty' 2>/dev/null || true)
    [ -n "$_cc_session_id" ] && echo "$_cc_session_id" > "$STATE_DIR/cc_session_id"
fi

# --- Resolve project + active vault ---
project_id=$(memex_resolve_project_id)
project_vault=$(memex_resolve_active_vault)

# Persist resolved vault so other hooks in the same session can read it
# without paying KV round-trip cost on every invocation.
echo "$project_vault" > "$STATE_DIR/active_vault"
echo "$project_id" > "$STATE_DIR/project_id"

# --- Install Tier 1b+2 agent surface as a project rule file ---
# Claude Code v2.1.x silently truncates SessionStart `additionalContext`
# above 10K chars (Anthropic-side #42369), so the agent surface cannot
# travel inline anymore. Instead we install it into `<project>/.claude/rules/`
# where the harness loads it as system-prompt content (no cap, cached). The
# CLI's --output-dir flag is atomic and skips the rewrite when content is
# unchanged. First-install only takes effect after the next session boot
# (the system prompt is assembled before SessionStart hooks fire), so
# surface a restart hint when we just created the file. Failures (e.g.
# older CLI without --output-dir) are kept silent in the systemMessage but
# breadcrumb'd to $STATE_DIR/agent_surface_install.err so operators can
# diagnose silent installs.
agent_surface_install_warning=""
if [ -n "$_project_root" ]; then
    rules_dir="${_project_root}/.claude/rules"
    rules_path="${rules_dir}/memex-agent-surface.md"
    rules_existed_before=0
    [ -f "$rules_path" ] && rules_existed_before=1
    if memex agent-surface claude-code --output-dir "$rules_dir" \
            >/dev/null 2>"$STATE_DIR/agent_surface_install.err"; then
        # Empty stderr on success — keep the state dir tidy.
        [ -s "$STATE_DIR/agent_surface_install.err" ] || rm -f "$STATE_DIR/agent_surface_install.err"
        if [ "$rules_existed_before" -eq 0 ] && [ -f "$rules_path" ]; then
            agent_surface_install_warning=" · Agent surface installed at .claude/rules/memex-agent-surface.md — restart Claude Code to load it"
        fi
    fi
fi

# --- Opt-out: MEMEX_CC_SESSION_BRIEFING=off|0|false|no|disabled skips the fetch ---
# Static vault/session/auto-tag instructions still emit; only the dynamic briefing
# markdown is suppressed. Agent-surface install above is unaffected.
_briefing_disabled=0
case "${MEMEX_CC_SESSION_BRIEFING:-on}" in
    off|0|false|no|disabled)
        _briefing_disabled=1
        ;;
esac

# --- Build briefing CLI args ---
# --app claude-code selects the app:claude-code procedural pin context
# (layered on global + project:<id>) so pinned procedure cards land in
# this briefing (V7 §19.8).
briefing_args=(briefing --budget 2000 --app claude-code)
[ -n "$project_vault" ] && briefing_args+=(--vault "$project_vault")
[ -n "$project_id" ] && briefing_args+=(--project-id "$project_id")

# --- Register cleanup trap upfront so any later failure cleans temp files.
# Guard with -n so the disabled-briefing path (tmp_briefing never assigned)
# doesn't spawn a noisy `rm -f ""` warning on exit. ---
tmp_briefing=''
tmp_briefing_err=''
trap '[ -n "$tmp_briefing" ] && rm -f "$tmp_briefing"; [ -n "$tmp_briefing_err" ] && rm -f "$tmp_briefing_err"' EXIT

# --- Fetch dynamic session briefing (per-vault state from the server) ---
briefing_content=""
if [ "$_briefing_disabled" -eq 0 ]; then
    tmp_briefing=$(mktemp)
    tmp_briefing_err=$(mktemp)

    if ! memex "${briefing_args[@]}" > "$tmp_briefing" 2>"$tmp_briefing_err"; then
        # Don't blame the server for every failure. An old `memex` that doesn't
        # understand a newer plugin's flags (e.g. `--app`), a missing CLI, or bad
        # args all exit non-zero too — the old code piped stderr to /dev/null and
        # always printed "server unreachable", which sent debugging down the wrong
        # path. Classify from CLI presence + the captured stderr instead.
        if ! command -v memex >/dev/null 2>&1; then
            cat <<'EOF'
{"systemMessage": "Memex CLI not found on PATH — MCP tools will not work. Install or upgrade it (e.g. `uv tool upgrade memex` / `pipx upgrade memex`)."}
EOF
        elif grep -qiE 'no such option|no such command|unexpected extra argument|missing argument|usage:' "$tmp_briefing_err"; then
            cat <<'EOF'
{"systemMessage": "Memex briefing failed: the installed `memex` CLI doesn't understand this plugin's briefing command — a plugin/CLI version mismatch (the plugin is newer than your CLI). Upgrade the CLI to match: `uv tool upgrade memex` (or `pipx upgrade memex`). Run `memex briefing --budget 2000 --app claude-code` to see the exact error."}
EOF
        else
            cat <<'EOF'
{"systemMessage": "Memex server is not reachable. Start it with:\n  memex server start -d\n\nMemex MCP tools will not work until the server is running. (If the server IS running, run `memex briefing --budget 2000 --app claude-code` manually to see the real error.)"}
EOF
        fi
        exit 0
    fi

    # The briefing now TRAILS the vault/session/auto-tag blocks (see the
    # assembly below), and those blocks end without a newline. Prepend a blank
    # line so the briefing's leading `# Session Briefing` heading starts on its
    # own line instead of gluing onto the end of the auto-tag paragraph. No
    # trailing newline is needed — nothing follows the briefing.
    briefing_content="

$(cat "$tmp_briefing")"
fi
status="🧠 Memex connected${agent_surface_install_warning}"
[ "$_briefing_disabled" -eq 1 ] && status="${status} · Briefing disabled (MEMEX_CC_SESSION_BRIEFING)"

if [ -n "$project_vault" ]; then
    vault_instruction="
### Per-project vault

This project uses vault \`${project_vault}\` (project: \`${project_id}\`). Pass \`vault_id: \"${project_vault}\"\` on Memex write calls (\`memex_add_note\`, \`memex_append_note\`). Read calls default to search vaults and generally do not need a vault_id override."
else
    vault_instruction="
### Per-project vault

No project-specific vault is configured (project: \`${project_id}\`). Notes will be written to the default vault. To bind this project to a specific vault, call \`memex_kv_put(key=\"app:claude-code:project:${project_id}:vault\", value=\"<vault_name>\")\`. This will take effect on the next session."

    status="${status} · No vault set — tell me which vault to use for this project"
fi

session_note_instruction="
### Session note

This session's note key is \`${SESSION_NOTE_KEY}\`. The plugin auto-captures the full session transcript to this note on exit, and appends pre-compaction context here. When you complete a meaningful unit of work, you can also extend it explicitly via \`memex_append_note(note_key=\"${SESSION_NOTE_KEY}\", delta=\"...\", background=true)\` — append a concise summary of what was done and why (don't re-send the whole body)."

auto_tag_instruction="
### Auto-injected metadata

Every \`memex_add_note\` call from this session is auto-tagged with: \`surface:claude-code\`, \`session:${SESSION_NOTE_KEY#session:}\`, \`project:${project_id}\`, plus git context (\`git:branch=...\`, \`git:sha=...\`, \`git:repo=...\`, \`git:dirty\` when applicable) and \`claude:model=...\`. \`background\` defaults to \`true\` unless you explicitly pass \`false\`. Pre-existing tags you supply are preserved."

# Order matters: Claude Code v2.1.x silently truncates `additionalContext`
# above 10K chars (see the agent-surface note above). The briefing can be up
# to ~2000 tokens (~8K chars), so if it led, the small but load-bearing
# instructions behind it — especially the per-project vault block that
# `/continue` and write calls depend on — could be cut off. Put the compact,
# always-needed blocks (vault, session note, auto-tag) FIRST so they always
# survive; the larger briefing trails and absorbs any truncation itself.
additional_context="${vault_instruction}${session_note_instruction}${auto_tag_instruction}${briefing_content}"

[ -n "$project_vault" ] && status="${status} (vault: ${project_vault})"

# --- Output JSON ---
jq -n \
    --arg sm "$status" \
    --arg ac "$additional_context" \
    '{
        systemMessage: $sm,
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ac
        }
    }'
