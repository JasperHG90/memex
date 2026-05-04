---
name: remember
description: "Save information to Memex long-term memory. Captures the given text (or infers the most important context from the conversation) as a persistent note."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex Long-Term Memory

1. **Determine content**: use `$ARGUMENTS` if provided; otherwise infer the most important persistable context from the conversation.

2. **Format**:
   - **title**: concise, ≤10 words
   - **markdown_content**: specific enough to be useful without the original conversation
   - **description**: one-sentence summary, ≤250 words
   - **author**: `"claude-code"`
   - **tags**: always include `"claude-code"` + `"manual-capture"` + 1-3 topic tags

3. **Pick the right surface** (see `.claude/rules/memory-layers.md`):
   - Note (`memex_add_note` / `memex_append_note`) for Episodic/Semantic/Conceptual content
   - `procedure:` KV namespace for Procedural-observations (learned how-tos) — see step 5

4. **Template for structured content**: `memex_list_templates` → `memex_get_template(slug)` → `memex_add_note(..., template=slug)`. Skip for short unstructured captures.

5. **Save**: call `memex_add_note` with `background: true`. Confirm to the user.

## Deprioritize vs archive

- `memex_memory_deprioritize(unit_id, reason)` — NON-DESTRUCTIVE. Lowers retrieval rank; unit stays accessible via `include_deprioritized=true`. Reversible via `memex_memory_restore`.
- Archive (`memex memory delete`, CLI-only) — DESTRUCTIVE, removes from entity graph. Prefer deprioritize unless PII removal is required.

## 5-step resolution flow (user reports issue fixed)

See `.claude/rules/memory-resolution-flow.md` for the canonical text. Short version:

1. **Disambiguate** — if scope ambiguous, ASK.
2. **Route** — title → `memex_find_note`; content → `memex_memory_search`. Then pick: (A) entity-anchored, (B) cross-note semantic (top_k≥30), or (C) single-note PageIndex.
3. **LLM-judge** — read candidate unit bodies, pick fix-relevant subset. Never bulk-write.
4. **+5. Paired writes** against judged subset: `memex_record_outcome(success=false)` AND `memex_memory_deprioritize(reason=...)`.

Historical/audit queries ("how has my view on X evolved") → `memex_get_unit_history` or `memex_memory_search(apply_pre_filter=False)`.

## Capturing a learned procedure (procedure: KV namespace)

For how-tos ("how I write PRs for this project"), write to `procedure:<verb>:<context-tag>`:
- Save: `memex_kv_write(value=..., key="procedure:<verb>:<context-tag>")`
- Read active value: `memex_kv_get(key)`; history: `memex_kv_get(key, include_history=true)`
- After USE, close the loop: `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`

Use procedure: keys for executable how-tos; use `memex_add_note` for facts/decisions/context.

## Consolidation verbs

- `memex_memory_summarize_node(entity_id, scope)` — synchronous reflection. `'incremental'` (default) for new evidence only; `'full'` to re-evaluate all (capped 1000 units). Rate-limited: 1 call per (entity, vault) per 60s.
- `memex_memory_reconsolidate(entity_id, vault_id)` — entity-scoped contradiction detection + reflection. Per-entity advisory lock.
- `memex_memory_consolidate(vault_id, dry_run)` — vault-scoped batch deprioritization of low-MW + stale units. Use sparingly (monthly).
