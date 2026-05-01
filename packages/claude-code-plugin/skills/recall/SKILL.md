---
name: recall
description: "Search Memex long-term memory for relevant information. Returns facts, notes, and entities matching the query."
argument-hint: "[search query]"
---

# /recall — Search Memex Long-Term Memory

You have been invoked via the `/recall` slash command.

## Instructions

1. **Determine the search query.**
   - Use `$ARGUMENTS` as the search query.
   - If `$ARGUMENTS` is empty, ask the user what they would like to recall.

2. **Search strategy** (two-stage with expansion fallback):
   a. **First pass**: call `memex_memory_search` AND `memex_note_search` in parallel (no expansion).
   b. **If insufficient**: retry both with `expand_query=true` for broader recall via LLM query expansion.
   c. If still no results, call `memex_list_entities` to browse the knowledge graph.
   d. If nothing is found after all three strategies, say so — do not guess.

3. **Present results.**
   - Summarize the findings in a clear, readable format.
   - Include source Note IDs so the user can drill deeper with `memex_read_note`.
   - If no results are found, tell the user and suggest alternative queries.

4. **Recall a learned procedure** (F14) — when the user asks "how do I
   X?", "what's our procedure for Y?", or you are about to perform a
   recurring task (writing a PR, running the test matrix), check the
   `procedure:` KV namespace BEFORE other surfaces:
   - First scan `memex_kv_list(namespaces=["procedure"])` for matching
     `procedure:<verb>:<context-tag>` keys.
   - Read with `memex_kv_get(key)` for the active value, or
     `memex_kv_get(key, include_history=true)` to also see the capped
     5-version history (useful when the active value looks stale).
   - After ACTUALLY using a procedure to perform the action, close the
     loop with
     `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`
     so the procedure's Memory Worth counters stay calibrated.

   Procedure recall is shape-different from note recall: it returns an
   instruction to execute, not a fact to remember. Reach for it whenever
   the user query is "how-to" rather than "what".

<!--
Tier A — /recall verb extensions
F8:  WS-linter      (get_lint_flags surfacing)
F20: WS-revisit     (get_due_for_review surfacing)
F32: WS-diagnostics (get_diagnostics_summary surfacing)

# --- F8 ---  (filled by WS-linter)

# --- F20 --- (filled by WS-revisit)
4. **Memories due for review** (when the user asks "what's due?", "what
   should I revisit?", "show my review queue", etc.): call
   `memex_get_due_for_review(vault_id?)` to list units whose
   `revisit_due_at <= now()` AND that pass the 5-gate eligibility
   predicate. The list returns `{unit_id, text_preview, revisit_due_at,
   intent_class}`. After the user reviews each one, call
   `memex_memory_review(unit_id, quality)` with `quality` ∈ {`'again'`,
   `'hard'`, `'good'`, `'easy'`} to advance the FSRS-5 schedule and
   record the outcome.

   Sticky-deprioritize: 5 consecutive `'again'` ratings auto-flips a
   unit to `is_deprioritized=true`. The `auto_deprioritized` field on
   the response signals when the gate just triggered — surface that to
   the user. Only `memex memory restore <id>` (CLI) flips the gate
   back; positive ratings do NOT auto-restore.

# --- F32 --- (filled by WS-diagnostics)
5. **Vault diagnostics** (when the user asks "how is this vault doing?",
   "what's in vault X?", "check vault health", or wants visualisation):
   call `memex_get_diagnostics_summary(vault_id=...)` to surface unit
   counts by status (active / stale / deprioritized), pending lint counts
   by type, cluster_count (null when the UMAP manifold cache is cold),
   avg MW score, and top retrieved entities. For full JSON or visual
   plots, point the user to the CLI: `memex diagnostics manifold |
   retrieval | summary --vault X`.
-->

<!--
F14: WS-quick-wins  (procedure: KV recall — see step 4 above)
-->
