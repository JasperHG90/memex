---
name: remember
description: "Save information to Memex long-term memory. Captures the given text (or infers the most important context from the conversation) as a persistent note."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex Long-Term Memory

You have been invoked via the `/remember` slash command.

## Instructions

1. **Determine what to remember.**
   - If `$ARGUMENTS` is provided and non-empty, use that text as the memory content.
   - If `$ARGUMENTS` is empty, review the recent conversation and identify the single
     most important piece of information worth persisting (e.g. a decision, a discovery,
     a user preference).

2. **Format the memory.**
   - **title**: A concise, descriptive title (≤10 words).
   - **markdown_content**: The memory body in Markdown. Be specific and include enough
     context so the memory is useful in a future session without the original conversation.
   - **description**: A one-sentence summary (≤250 words).
   - **author**: `"claude-code"`
   - **tags**: Always include `"claude-code"` and `"manual-capture"`. Add 1-3 additional
     topic tags derived from the content.

3. **Consider a template (for structured content).**
   If the memory is an architectural decision, technical brief, retro, or RFC,
   call `memex_list_templates` then `memex_get_template(slug)`, follow the
   structure when writing `markdown_content`, and pass `template: "<slug>"` to
   `memex_add_note`. Skip for short, unstructured captures.

4. **Save the memory.**
   Call the `memex_add_note` MCP tool with the values above and set `background: true`
   so ingestion does not block the conversation.

5. **Confirm to the user.**
   After calling the tool, briefly confirm what was saved and mention the title.

## Curating memory after the fact (deprioritize vs archive)

When a previously-saved memory turns out to be misleading, outdated, or noise
that contaminates retrieval, prefer the **NON-DESTRUCTIVE** verb:

- `memex_memory_deprioritize(unit_id, reason)` lowers the unit's retrieval
  rank without removing it from the entity graph. The unit remains accessible
  via `include_deprioritized=true` retrieval. Use the `reason` field liberally
  ("user confirmed issue fixed", "superseded by v2.3 release") — it is logged
  to `audit_logs` as the audit trail. Reversible via `memex_memory_restore`.
- Archive (CLI-only, `memex memory delete`) is the **DESTRUCTIVE** counterpart:
  it removes the unit from the entity graph and is irreversible. Prefer
  deprioritize unless the unit MUST leave the graph entirely.

## Capturing a learned procedure (procedure: KV namespace, F14)

Some kinds of "remembering" are NOT a note — they are a compact, learned
how-to: "how I write commit messages for this project", "how I run the
test matrix on this monorepo". For those, write to the `procedure:` KV
namespace instead of `memex_add_note`. Each procedure key is

```
procedure:<verb>:<context-tag>
```

— for example `procedure:write_pr:commit-style` or
`procedure:run_tests:python-monorepo`. The agent owns the verb (the
action you are taking); Memex stores observations about how to ADAPT the
verb to a specific context (the context-tag).

Use `memex_kv_write(value=..., key="procedure:<verb>:<context-tag>")` to
save. The server keeps the active value plus a capped 5-version history,
so an updated procedure overwrites the active value but does not lose
prior versions — read them with
`memex_kv_get(key, include_history=true)`.

After actually USING a procedure key in a turn (you read it via
`memex_kv_get` and then performed the action), close the loop with
`memex_record_outcome(target_type="kv_key", kv_key=..., success=...)` so
Memex's per-(vault, key) Memory Worth counters reflect what worked. The
counters drive the F14 briefing surface — silence provides no learning
signal.

Use a procedure: key when the content is a how-to that you (or a future
agent) will ACTUALLY EXECUTE; use `memex_add_note` when the content is a
fact, decision, or piece of context to recall.

## Synchronously consolidating mid-conversation (summarize_node vs reflect)

When you notice mid-conversation that retrieved facts about a topic are
conflicting, incomplete, or scattered, you can ask Memex to consolidate them
into a coherent mental model **before continuing**:

- `memex_memory_summarize_node(entity_id, scope)` triggers reflection
  **synchronously**. `scope='incremental'` (default) consolidates only new
  evidence; `scope='full'` re-evaluates all evidence on the entity (capped
  at the most-recent 1000 units). The tool returns the updated mental model
  in the same turn, so you can act on it immediately.
- Background `reflect` (the existing scheduler-driven path) is the **default**:
  it runs asynchronously when leader-elected and is the cheaper option.

`summarize_node` is rate-limited to **1 call per (entity, vault) per 60
seconds**. If you hit the limit, the response includes `retry_after_seconds`;
do not retry-loop. Reach for `summarize_node` only when an in-session reason
exists (a contradiction signal, a user-driven question that depends on the
consolidated view); otherwise let background reflection do its work.

## Reconsolidating versus consolidating (F9)

Two related but **distinct** curation verbs:

- `memex_memory_reconsolidate(entity_id, vault_id)` is **ENTITY-SCOPED**.
  Use when retrieved facts about a specific entity disagree. Runs
  contradiction detection across that entity's linked units, then reflection.
  Acquires a per-entity Postgres advisory lock — concurrent calls on the
  same entity serialise (the second observes a `lock_contention` envelope).
- `memex_memory_consolidate(vault_id, dry_run)` is **VAULT-SCOPED**.
  Identifies low-MW + stale units across the entire vault and deprioritizes
  them; writes findings to the maintenance ledger. Use sparingly (e.g.,
  monthly per vault). `dry_run=true` returns the candidate list as a preview
  without writes.

Reach for `reconsolidate` on concrete contradiction signals; `consolidate`
is the periodic batch. Both are LLM-intensive and write-side — prefer
`memex_memory_deprioritize` for individual high-confidence noise units.

<!--
Tier A — /remember verb extensions
F4:  WS-quick-wins  (memory_deprioritize/restore disclosure)
F5:  WS-quick-wins  (memory_summarize_node disclosure)
F9:  WS-locks       (memory_reconsolidate, memory_consolidate disclosure)
F14: WS-quick-wins  (procedural KV capture surfacing)
F20: WS-revisit     (memory_review disclosure)

# --- F4 ---  (filled by WS-quick-wins — see "Curating memory after the fact" section above)

# --- F5 ---  (filled by WS-quick-wins)

# --- F9 ---  (filled by WS-locks)

# --- F14 --- (filled by WS-quick-wins — see "Capturing a learned procedure" section above)

# --- F20 --- (filled by WS-revisit)
-->
