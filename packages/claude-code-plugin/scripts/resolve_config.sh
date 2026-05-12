#!/usr/bin/env bash
# resolve_config.sh — Memex CLI wrapper + project / vault resolution helpers.
#
# Source this from hook scripts. Defines:
#   memex                           — wrapper around the uvx-pinned Memex CLI.
#   memex_resolve_project_id        — derive a portable project identifier.
#   memex_resolve_active_vault      — Hermes-style hierarchical vault lookup.
#   memex_kv_namespace_migrate      — one-time migration of the project vault key.

# When sourced from a hook that emits its own JSON on stdout (e.g., a
# PreToolUse hook returning ``updatedInput``), an early system message here
# would corrupt the hook output. Surface uvx-missing or bad-version errors
# *only* when the caller explicitly opts in via MEMEX_RESOLVE_VERBOSE=1, or
# from the SessionStart hook (where the message is the only output anyway).
_memex_emit_systemMessage() {
    if [ "${MEMEX_RESOLVE_VERBOSE:-0}" = "1" ]; then
        cat
    fi
}

if ! command -v uvx >/dev/null 2>&1; then
    _memex_emit_systemMessage <<'EOF'
{"systemMessage": "❌ `uvx` is not on PATH. Hooks require it to run the Memex CLI.\n\nInstall uv: https://docs.astral.sh/uv/getting-started/installation/"}
EOF
    # Stub out memex so callers can still source us safely; calls just fail.
    memex() { return 1; }
    return 0 2>/dev/null || exit 0
fi

_memex_ref="${MEMEX_PLUGIN_VERSION:-latest}"
_memex_pkg="memex-cli @ git+https://github.com/JasperHG90/memex.git@${_memex_ref}#subdirectory=packages/cli"

# Validate a non-`latest` ref against the remote, with a day-grained on-disk
# cache so we don't hit `git ls-remote` on every hook invocation. The remote
# tag/branch list does not change minute-to-minute from a hook's perspective;
# a 24-hour cache TTL is fine. The previous unconditional `git ls-remote`
# made a network call on EVERY PreToolUse / PreCompact / SessionEnd when the
# user pinned MEMEX_PLUGIN_VERSION to a specific tag.
_memex_validate_ref() {
    local _ref="$1"
    local _state_dir="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
    local _cache_dir="${_state_dir}/refcache"
    local _safe_ref
    _safe_ref=$(printf '%s' "$_ref" | tr -c 'A-Za-z0-9._-' '_')
    local _cache_file="${_cache_dir}/${_safe_ref}"

    if [ -f "$_cache_file" ]; then
        # Fresh = < 24h old. `find -mmin +1440` matches files OLDER than 24h;
        # if it matches nothing, the cache is fresh.
        if ! find "$_cache_file" -mmin +1440 2>/dev/null | grep -q .; then
            case "$(cat "$_cache_file" 2>/dev/null)" in
                ok) return 0 ;;
                bad) return 1 ;;
            esac
        fi
    fi

    mkdir -p "$_cache_dir" 2>/dev/null || true
    # Bound the network call: SessionStart is latency-sensitive and a hung
    # DNS / proxy could otherwise stall it indefinitely. 5s is plenty for
    # an `ls-remote` against a single ref. Match the gtimeout-on-macOS
    # probe used by `memex()` so this works on Apple machines too.
    local _ls_cmd=(git ls-remote --tags --heads https://github.com/JasperHG90/memex.git "$_ref")
    local _ls_status=0
    if command -v timeout >/dev/null 2>&1; then
        timeout 5 "${_ls_cmd[@]}" 2>/dev/null | grep -q . || _ls_status=$?
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout 5 "${_ls_cmd[@]}" 2>/dev/null | grep -q . || _ls_status=$?
    else
        "${_ls_cmd[@]}" 2>/dev/null | grep -q . || _ls_status=$?
    fi
    if [ "$_ls_status" -eq 0 ]; then
        echo ok > "$_cache_file" 2>/dev/null || true
        return 0
    fi
    # Don't cache "bad" on a timeout (exit 124) — the ref may be valid and
    # the network just hung. Only cache deterministic "ref doesn't exist"
    # verdicts so transient outages don't pin a false negative for 24h.
    if [ "$_ls_status" -ne 124 ]; then
        echo bad > "$_cache_file" 2>/dev/null || true
    fi
    return 1
}

if [ "$_memex_ref" != "latest" ]; then
    if ! _memex_validate_ref "$_memex_ref"; then
        # Build the diagnostic JSON via `printf` + `jq --arg` rather than an
        # unquoted heredoc. The previous `<<EOF` form would expand `$(...)`
        # / backticks embedded in MEMEX_PLUGIN_VERSION (so a contributor
        # mistyping `MEMEX_PLUGIN_VERSION='$(date)'` would silently see
        # `MEMEX_PLUGIN_VERSION='<today>'`) and would corrupt JSON if the
        # value contained `"`. `printf '%s' "$var"` substitutes the value
        # as opaque text, and `jq --arg` JSON-escapes it.
        if [ "${MEMEX_RESOLVE_VERBOSE:-0}" = "1" ] && command -v jq >/dev/null 2>&1; then
            _diag_msg=$(printf "❌ MEMEX_PLUGIN_VERSION='%s' does not exist as a tag or branch on github.com/JasperHG90/memex.\n\nAvailable tags: https://github.com/JasperHG90/memex/tags\n\nUnset the variable to use the default (latest)." "$_memex_ref")
            jq -n --arg msg "$_diag_msg" '{systemMessage: $msg}'
            unset _diag_msg
        fi
        memex() { return 1; }
        return 0 2>/dev/null || exit 0
    fi
fi

# Wrap CLI calls in a hard timeout so a hung server can't block hooks
# indefinitely. SessionEnd / PreCompact are particularly sensitive: the
# Claude Code host may force-kill ``async`` hooks after ~10s, so we cap our
# call latency well below that. Individual call sites can override via
# MEMEX_CC_TIMEOUT (seconds).
memex() {
    local _timeout="${MEMEX_CC_TIMEOUT:-8}"
    # Validate: positive integer in [1..600]. Anything else falls back to 8s
    # so a misconfigured env var cannot turn `timeout` into an invalid-arg
    # error (exit 125), which is indistinguishable from a real CLI failure.
    case "$_timeout" in
        ''|*[!0-9]*) _timeout=8 ;;
    esac
    # Reject obvious overflow before bash's [ -gt ] hits its INT64 ceiling.
    [ "${#_timeout}" -gt 4 ] && _timeout=8
    [ "$_timeout" -lt 1 ]    && _timeout=8
    [ "$_timeout" -gt 600 ]  && _timeout=600

    # macOS doesn't ship GNU coreutils — `timeout(1)` is absent there but
    # `gtimeout(1)` is the homebrew/coreutils name. Probe both before
    # falling back to an unbounded call so a hung server can't quietly
    # stall hooks on Apple machines.
    if command -v timeout >/dev/null 2>&1; then
        timeout "$_timeout" uvx --from "$_memex_pkg" memex "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$_timeout" uvx --from "$_memex_pkg" memex "$@"
    else
        uvx --from "$_memex_pkg" memex "$@"
    fi
}

# ---------------------------------------------------------------------------
# Git remote URL normalisation
#
# Shared by `memex_resolve_project_id` (KV key) and `inject_memex_tags.sh`
# (git:repo tag). Both want the same `host/owner/repo` shape regardless of
# whether the remote is HTTPS, basic-auth HTTPS, SCP-style SSH, or `ssh://`.
# Worked examples:
#
#   https://github.com/acme/myapp.git           → github.com/acme/myapp
#   https://oauth2:t0k@github.com/acme/myapp    → github.com/acme/myapp
#   git@github.com:acme/myapp.git               → github.com/acme/myapp
#   ssh://git@github.com/acme/myapp.git         → github.com/acme/myapp
#   https://gitlab.com/org/subgroup/repo.git    → gitlab.com/org/subgroup/repo
#
# Pipeline:
#   1. strip trailing `.git`
#   2. strip HTTPS basic-auth (`user:password@`)
#   3. strip `<scheme>://`
#   4. strip an explicit `:NNNN/` port from the host segment so SSH URLs
#      like `ssh://git@github.com:22/acme/myapp` don't leak the port into
#      the project ID. This MUST run before step 5 — otherwise step 5
#      would treat the port-colon as the SCP user@host: separator and
#      drag the port digits into the path.
#   5. collapse `user@host[:/]path` → `host/path` (handles both the SCP-style
#      colon separator and the post-scheme-strip slash)
# ---------------------------------------------------------------------------
memex_normalize_git_remote_url() {
    printf '%s' "$1" | sed '
        s/\.git$//
        s|https://[^@]*@|https://|
        s|^[a-zA-Z][a-zA-Z0-9+.-]*://||
        s|@\([^:/]*\):[0-9][0-9]*/|@\1/|
        s|^[^@]*@\([^:/]*\)[:/]|\1/|
    '
}

# ---------------------------------------------------------------------------
# Project identifier
#
# Stable across machines: prefer the git remote (normalized via
# `memex_normalize_git_remote_url`), fall back to a path relative to $HOME,
# then $PWD.
# ---------------------------------------------------------------------------
memex_resolve_project_id() {
    local _id=""
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local _remote
        _remote=$(git remote get-url origin 2>/dev/null) || true
        if [ -n "$_remote" ]; then
            _id=$(memex_normalize_git_remote_url "$_remote")
        fi
    fi
    if [ -z "$_id" ]; then
        local _home="${HOME:-}"
        case "$PWD" in
            "${_home}"/*) _id="${PWD#"$_home"/}" ;;
            *)            _id="$PWD" ;;
        esac
    fi
    printf '%s' "$_id"
}

# ---------------------------------------------------------------------------
# KV helpers
# ---------------------------------------------------------------------------
_memex_kv_get_value() {
    # $1: key. Echoes the value (or empty string) on stdout, never errors.
    memex kv get "$1" --value-only 2>/dev/null || true
}

_memex_kv_write_value() {
    # $1: value, $2: key. Returns 0 on success, non-zero on failure.
    memex kv write "$1" --key "$2" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# KV namespace migration
#
# CC plugin historically wrote `project:<id>:vault`. The plugin now namespaces
# under `app:claude-code:project:<id>:vault` to mirror the Hermes plugin's
# `app:hermes:*` discipline and avoid collisions with other tools.
#
# On first call with a legacy bare key, copy its value to the new key. The
# bare key is left in place — deletions in KV are user-only — but the
# new key wins on subsequent reads.
#
# Echoes the resolved value on stdout (empty string if neither key is set).
# ---------------------------------------------------------------------------
memex_kv_namespace_migrate() {
    local _project_id="$1"
    [ -z "$_project_id" ] && return 0

    local _new_key="app:claude-code:project:${_project_id}:vault"
    local _old_key="project:${_project_id}:vault"

    local _new_val
    _new_val=$(_memex_kv_get_value "$_new_key")
    if [ -n "$_new_val" ]; then
        printf '%s' "$_new_val"
        return 0
    fi

    local _old_val
    _old_val=$(_memex_kv_get_value "$_old_key")
    if [ -n "$_old_val" ]; then
        # Forward-migrate. If the write fails (server down, missing perms),
        # the next session will retry; the legacy key remains as a safety
        # net. Log the failure to stderr so it surfaces in CC's hook trace.
        if ! _memex_kv_write_value "$_old_val" "$_new_key"; then
            echo "memex resolve_config: forward-migration of '${_old_key}' → '${_new_key}' failed; continuing with legacy value. Will retry next session." >&2
        fi
        printf '%s' "$_old_val"
        return 0
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Hierarchical active vault resolver
#
# Order (mirrors Hermes' chain at packages/hermes-plugin/src/memex_hermes_plugin/memex/project.py):
#   1. app:claude-code:project:<project_id>:vault   (project binding)
#   2. app:claude-code:user:$USER:vault             (per-user default)
#   3. app:claude-code:agent:<agent_id>:vault       (per-subagent — only if MEMEX_CC_AGENT_ID is set)
#   4. $MEMEX_VAULT environment variable
#   5. server-side default (echoes empty; the server picks the configured default)
#
# In-process result cached in MEMEX_CC_RESOLVED_VAULT to avoid repeated KV
# round-trips within a single hook invocation.
# ---------------------------------------------------------------------------
memex_resolve_active_vault() {
    if [ -n "${MEMEX_CC_RESOLVED_VAULT:-}" ]; then
        printf '%s' "$MEMEX_CC_RESOLVED_VAULT"
        return 0
    fi

    local _project_id
    _project_id=$(memex_resolve_project_id)

    local _vault=""

    # 1. Project-level (with one-time legacy migration)
    if [ -n "$_project_id" ]; then
        _vault=$(memex_kv_namespace_migrate "$_project_id")
    fi

    # 2. User-level
    if [ -z "$_vault" ] && [ -n "${USER:-}" ]; then
        _vault=$(_memex_kv_get_value "app:claude-code:user:${USER}:vault")
    fi

    # 3. Agent-level (only if subagent identity is set)
    if [ -z "$_vault" ] && [ -n "${MEMEX_CC_AGENT_ID:-}" ]; then
        _vault=$(_memex_kv_get_value "app:claude-code:agent:${MEMEX_CC_AGENT_ID}:vault")
    fi

    # 4. Environment override
    if [ -z "$_vault" ] && [ -n "${MEMEX_VAULT:-}" ]; then
        _vault="$MEMEX_VAULT"
    fi

    # 5. Server default — leave empty.

    export MEMEX_CC_RESOLVED_VAULT="$_vault"
    printf '%s' "$_vault"
}

# ---------------------------------------------------------------------------
# Session-note persistence (used by PreCompact and SessionEnd)
#
# memex_persist_session_delta <state_dir> <cc_session_id> <session_note_key>
#                             <vault> <title> <description> <delta_markdown>
#                             [<extra_tag> ...]
#
# Behavior:
#   - First call (state file absent): `memex note add` with full content.
#   - Subsequent calls: `memex note append` with the delta.
#   - State tracked via $state_dir/session_note_created_<safe_session_id>.
#
# Returns 0 on success, non-zero on failure. Caller decides what to surface.
#
# Concurrency assumption:
#   PreCompact (sync) and SessionEnd (async) callers share `_flag_file` as
#   the "note created" sentinel. The flag is written AFTER `memex note add`
#   succeeds, leaving a small window where the CLI has succeeded but the
#   flag is absent on disk. Under Claude Code's hook ordering, PreCompact
#   completes before SessionEnd is dispatched, so the window is not
#   observable in practice. If a future host runs the two hooks
#   concurrently, this needs a `flock` (or a server-side
#   "create-if-missing" path) to remain safe. Documented to keep future
#   refactors honest.
# ---------------------------------------------------------------------------
memex_persist_session_delta() {
    local _state_dir="$1"; shift
    local _cc_session_id="$1"; shift
    local _note_key="$1"; shift
    local _vault="$1"; shift
    local _title="$1"; shift
    local _description="$1"; shift
    local _delta="$1"; shift
    # Remaining args are extra tags.

    if [ -z "$_note_key" ]; then
        return 1
    fi
    if [ -z "$_delta" ]; then
        return 1
    fi

    local _safe_session_id
    _safe_session_id=$(printf '%s' "$_cc_session_id" | tr -c 'A-Za-z0-9._-' '_')
    local _flag_file="${_state_dir}/session_note_created_${_safe_session_id}"

    if [ ! -f "$_flag_file" ]; then
        # First call: create the note. Flags first, then `--`, then the
        # positional `_delta` — transcript content can plausibly start with
        # `-` (e.g. a bash log line) and would otherwise be parsed as a CLI
        # flag.
        local _add_args=(note add --key "$_note_key" --background)
        [ -n "$_vault"       ] && _add_args+=(--vault "$_vault")
        [ -n "$_title"       ] && _add_args+=(--title "$_title")
        [ -n "$_description" ] && _add_args+=(--description "$_description")
        _add_args+=(--author "claude-code")
        for _tag in "$@"; do
            [ -n "$_tag" ] && _add_args+=(--tag "$_tag")
        done
        _add_args+=(-- "$_delta")

        if memex "${_add_args[@]}" >/dev/null 2>&1; then
            # Atomic flag write: create a sibling temp file, then rename.
            # `mv` is atomic on the same filesystem so a crash leaves either
            # the temp file (cleaned up at next SessionStart) or the final
            # flag (correctly marking the note as created). The old
            # `: > "$_flag_file"` could leave an empty partial file on a
            # crash that suppressed future appends.
            local _flag_tmp
            if _flag_tmp=$(mktemp "${_flag_file}.XXXXXX" 2>/dev/null); then
                if mv -f "$_flag_tmp" "$_flag_file" 2>/dev/null; then
                    return 0
                fi
                rm -f "$_flag_tmp"
            fi
            # Fall back to non-atomic write if mktemp/mv unavailable —
            # better than failing the whole persist.
            : > "$_flag_file"
            return 0
        fi
        return 1
    fi

    # Subsequent calls: append delta.
    local _append_args=(note append --key "$_note_key" --quiet)
    [ -n "$_vault" ] && _append_args+=(--vault "$_vault")

    if printf '%s' "$_delta" | memex "${_append_args[@]}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}
