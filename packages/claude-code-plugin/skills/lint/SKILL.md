---
name: lint
description: "Review and resolve Memex memory-hygiene findings — stale facts, near-duplicates, contradictions, orphaned mental models, mis-routed notes. Use when the user says 'lint my memory', 'clean up stale/duplicate memories', 'triage the maintenance ledger', or wants to work through the linter's pending findings. The agent explains each finding and, with your go-ahead, resolves or dismisses it."
argument-hint: "[optional: lint type — structural|quality|governance|schema|routing]"
---

# /lint — Triage the memory maintenance ledger

You have been invoked via the `/lint` slash command.

The linter scans the vault and files **advisory** findings (nothing is auto-applied). Your job: surface each pending finding, **explain what it is and why it matters**, recommend a verdict, and — once the user agrees — resolve or dismiss it. A human approves every destructive write; that is what keeps agent-driven resolution "attended."

## 1. List pending findings

Call `memex_get_lint_flags(lint_type=<from $ARGUMENTS if given>, status="pending", limit=…)`. It defaults to the active write vault (never global). Each finding carries `finding_id`, `target_id`, `target_type` (`memory_unit` / `note` / `mental_model` / `entity` / `kv` / `unit_entity`), `lint_type`, `evidence`, `suggested_action`, and `target_text`/`target_label`.

Findings are cursor-paginated — you only see the loaded window. If you summarize counts or offer "resolve all of rule X", say explicitly that it covers the loaded page, not the whole vault.

## 2. Load the action catalogue

Call `memex_list_lint_actions()` to get the closed catalogue (each action's `applicable_target_types`, `reversible`, and `params_schema`). For a given finding, only offer actions whose `applicable_target_types` include its `target_type`.

## 3. Per finding — explain, recommend, ask

Work one finding at a time. For each:

1. **Explain** it in plain terms: what the target is (`target_text`/`target_label`), what the linter flagged (`evidence`), and *why it matters* for memory quality. This reasoning is the value `/lint` adds over the raw cockpit.
2. **Recommend** a verdict (which action, or dismiss) with a one-line rationale.
3. **Ask** with `AskUserQuestion`: **resolve (with the action)** / **dismiss** / **skip**.

## 4. Apply the verdict

- **Contradiction winner-proposals** (`propose_contradiction_winner`) are MCP-native:
  - apply → `memex_lint_apply_winner(finding_id)`
  - undo → `memex_lint_reverse_winner(finding_id)`
- **Everything else** (generic resolve/dismiss has no MCP verb) → shell the `memex` CLI via `Bash`. Mirror the plugin's launcher: if `MEMEX_LOCAL_PATH` is set use the dev form, else the installed `memex` on PATH.
  - resolve with an action → `memex lint resolve <finding_id> --action <action_id> --params '<json>' --note "<why>"`
  - pure accept (status flip only) → `memex lint resolve <finding_id> --note "<why>"`
  - dismiss → `memex lint dismiss <finding_id> --note "<why>"`
  - undo a reversible action → `memex lint reverse <finding_id>`

prod form: `memex lint resolve …` (the `memex` you installed via `uv tool install`, on PATH)
dev form (`MEMEX_LOCAL_PATH` set): `uv run --project "$MEMEX_LOCAL_PATH" --package memex-cli memex lint resolve …`

## 5. The unattended-apply gate

A canned-action `resolve --action` is a **destructive** mutation. The gate is enforced **server-side**, so it fires the same way whether you call via the CLI or MCP — including `memex_lint_apply_winner`: on a local server with `server.auth.enabled=False` and `MEMEX_LINT_ALLOW_UNATTENDED_APPLY` unset, the call returns **HTTP 403**. (Action-less `resolve` and `dismiss` are *not* gated.) On a 403, tell the user to restart the server with `MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1` and retry — do not try to route around it.

## 6. Report

Summarize what was resolved, dismissed, and skipped (with the loaded-window caveat if relevant), and note that reversible resolutions can be undone with `memex lint reverse <finding_id>`.
