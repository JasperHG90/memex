"""Canonical per-tool description text for the Memex MCP tools.

This module is the **single source of truth** for Tier 1a per-tool
description strings — what each tool does + its required-param shape +
4xx-triggering invariants. Both the MCP server (which serves these as
tool-discovery descriptions to MCP clients) and the Hermes plugin (which
mirrors the same tool schemas in-process) import from here.

See `CLAUDE.md` §"Agent-surface architecture" for the three-tier model.
This module is **Tier 1a**; the universal system-prompt content (5-step
flow, axes table, retrieval routing, etc.) lives in
`memex_common.agent_surface` (Tier 1b) and is composed into agent
system prompts, NOT duplicated per tool here.

Style discipline (Tier 1a):
1. Concrete, imperative; what + required params + 4xx triggers.
2. One brief "when to use" line per tool — strategic guidance, NOT
   multi-step composition (that's Tier 1b).
3. ≤300 tokens / ≤1,200 chars per constant.
4. Inline `<example>` blocks only for tools with ambiguous shapes.

Drift prevention: when the constant changes here, both MCP and Hermes
get the update on the next import. The verbatim test in
`packages/common/tests/test_tool_descriptions.py` pins the constants
so an unintended edit fails CI rather than diverging silently.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Outcome verbs (record_outcome / deprioritize / restore).
# ---------------------------------------------------------------------------

MEMEX_RECORD_OUTCOME_DESC = (
    'Record how previously retrieved memories contributed to the outcome.\n'
    '\n'
    'Shape: units=[{unit_id: UUID, verb: "helpful"|"not_helpful"|"not_used", '
    'reason: str|null}].\n'
    'Required: `reason` for helpful and not_helpful; optional for not_used.\n'
    'Invalid: bare `success=True` (or any bare flag) without `units` for '
    'target_type="memory_unit". Server returns HTTP 400.\n'
    '\n'
    'Alternate target: target_type="kv_key" + kv_key="procedure:<verb>:<context-tag>" '
    'to score a stored procedure rather than memory units.\n'
    '\n'
    'When to call: after you actually used the retrieved memory (or procedure) '
    'in your answer. Stamp the units you cited; do not bulk-write across the '
    'unfiltered candidate set.\n'
    '\n'
    '<example>units=[{"unit_id": "...", "verb": "helpful", "reason": '
    '"named the failing module"}, {"unit_id": "...", "verb": "not_used", '
    '"reason": null}]</example>'
)


MEMEX_MEMORY_DEPRIORITIZE_DESC = (
    "Lower a memory unit's retrieval rank without deleting it (NON-DESTRUCTIVE).\n"
    '\n'
    'Use when a memory is misleading, outdated, or noise contaminating retrieval.\n'
    'Required: unit_id (UUID), reason (free text, logged to maintenance ledger).\n'
    '\n'
    'Pair with memex_record_outcome(units=[{verb:"not_helpful", reason}]) when '
    'the user signals the memory was wrong — different axes (record_outcome = '
    'append-only MW counter; deprioritize = binary surface state). Reversible '
    'via memex_memory_restore.\n'
    '\n'
    'Invalid: virtual units (`unit_metadata.virtual: true`) have no DB row — '
    'returns HTTP 404. Filter candidates to `virtual` unset/false BEFORE calling; '
    'fall back to entity-anchored search (memex_list_entities → '
    'memex_get_entity_mentions) to recover real source units.\n'
    '\n'
    'Contrast with archive (CLI-only, destructive) — prefer deprioritize unless '
    'the unit must leave the entity graph entirely.'
)


MEMEX_MEMORY_RESTORE_DESC = (
    'Restore a previously-deprioritized memory unit (flip `is_deprioritized` to '
    'false). The unit re-enters default-scope retrieval. Writes an audit_logs row.\n'
    '\n'
    'Required: unit_id (UUID).\n'
    'Companion to memex_memory_deprioritize.'
)


# ---------------------------------------------------------------------------
# Reflection / consolidation.
# ---------------------------------------------------------------------------

MEMEX_MEMORY_SUMMARIZE_NODE_DESC = (
    'Trigger reflection SYNCHRONOUSLY on a specific entity or note set — the '
    'synchronous counterpart to the background reflect loop.\n'
    '\n'
    'Use when retrieved facts about a topic conflict, are incomplete, or are '
    'scattered, and you want Memex to consolidate them into a coherent mental '
    'model before continuing.\n'
    '\n'
    'Params: entity_id (focus on one entity — preferred) OR note_ids (focus on a '
    'specific set). scope="incremental" (default — only new evidence) or "full" '
    '(re-evaluate all, capped at 1000 units).\n'
    '\n'
    'Returns: ReflectionResult with updated/new MentalModel(s).\n'
    '\n'
    'Rate-limited per (entity, vault). On rejection the response carries '
    '`retry_after_seconds` — honor it rather than retry-looping.\n'
    '\n'
    'Use sparingly: reflection is LLM-intensive. Default to background reflection '
    'unless you have a specific in-session reason to trigger now.'
)


MEMEX_MEMORY_RECONSOLIDATE_DESC = (
    'Reconsolidate a single entity: re-run contradiction detection across its '
    'memory units and refresh its mental model. Entity-scoped operation.\n'
    '\n'
    'Use when you observe concrete contradiction signals on the entity '
    '(e.g. two memory units about Project X with opposite claims), or after '
    'ingesting a note that supersedes earlier claims.\n'
    '\n'
    'Required: entity_id, vault_id.\n'
    '\n'
    'Contrast with memex_memory_consolidate which is vault-scoped (batch '
    'deprioritizes low-MW + stale units, writes maintenance ledger). Use '
    '`reconsolidate` on concrete contradiction signals; use `consolidate` for '
    'periodic vault-wide maintenance.'
)


MEMEX_MEMORY_CONSOLIDATE_DESC = (
    'Run vault-wide memory consolidation: batch-deprioritize low-MW + stale '
    'units, write a maintenance ledger entry.\n'
    '\n'
    'Use for periodic vault maintenance, not for in-session conflict resolution '
    '(see memex_memory_reconsolidate for entity-scoped contradiction handling).\n'
    '\n'
    'Required: vault_id. Optional: dry_run=true to preview without applying.\n'
    '\n'
    'Rate-limited; honor `retry_after_seconds` on the response if rejected.'
)


# ---------------------------------------------------------------------------
# KV store.
# ---------------------------------------------------------------------------

MEMEX_KV_WRITE_DESC = (
    'Write a namespaced operational pointer to the KV store — a preference, '
    'project binding, convention, or learned procedure observation.\n'
    '\n'
    'NOT for content facts (events, observations, claims learned from notes) — '
    'those become memory units when you call memex_add_note. KV is for '
    'operational state the agent reads back across sessions.\n'
    '\n'
    'Required: value (str), key (str). The key MUST match the namespace regex: '
    '`^(global:|user:|project:<id>:|app:<app-id>:|procedure:<verb>:<context-tag>$)`. '
    'Server rejects invalid prefixes with HTTP 400.\n'
    '\n'
    'When to call: proactively, when the user says "remember…", "I prefer…", '
    '"we use…", "default to…". Namespace selection is in the system prompt '
    '§KV_NAMESPACE — scope qualifier wins over grammatical person.\n'
    '\n'
    'Optional: ttl_seconds (entry auto-expires; omit for no expiration). '
    'Generates a semantic embedding for fuzzy lookup via memex_kv_search.\n'
    '\n'
    'Procedure pairing: for `procedure:<verb>:<context-tag>` keys, pair every '
    'use with memex_record_outcome(target_type="kv_key", kv_key=...) so the '
    'procedure carries an MW trail.\n'
    '\n'
    'Deletion is CLI-only (`memex kv delete`); do NOT attempt to delete entries.'
)


MEMEX_KV_GET_DESC = (
    'Get a KV entry by exact key.\n'
    '\n'
    'Required: key (str — must include namespace prefix).\n'
    'For `procedure:` keys: the default response value is the unwrapped active '
    'procedure text. Pass `include_history=true` to receive the structured '
    'envelope ({value, version, history}) for reviewing prior versions.\n'
    '\n'
    'Returns 404 if the key does not exist. Use memex_kv_search for fuzzy lookup.'
)


MEMEX_KV_SEARCH_DESC = (
    'Semantic-search the KV store. Returns entries ranked by semantic similarity '
    'to the query against the embedded value strings.\n'
    '\n'
    'Required: query (str).\n'
    'Optional: top_k (default 10), prefix (e.g. "procedure:" to scope to '
    'learned how-tos).\n'
    '\n'
    'Use when you do not know the exact key. For exact-key lookup, use '
    'memex_kv_get.'
)


MEMEX_KV_LIST_DESC = (
    'Enumerate KV entries.\n'
    '\n'
    'Optional: prefix (filter to a namespace, e.g. "user:", "project:<id>:"), '
    'limit (default 100), offset (pagination).\n'
    '\n'
    'Returns key+value+scope per entry; values are NOT semantically reranked '
    '(use memex_kv_search for that).'
)


__all__ = [
    'MEMEX_KV_GET_DESC',
    'MEMEX_KV_LIST_DESC',
    'MEMEX_KV_SEARCH_DESC',
    'MEMEX_KV_WRITE_DESC',
    'MEMEX_MEMORY_CONSOLIDATE_DESC',
    'MEMEX_MEMORY_DEPRIORITIZE_DESC',
    'MEMEX_MEMORY_RECONSOLIDATE_DESC',
    'MEMEX_MEMORY_RESTORE_DESC',
    'MEMEX_MEMORY_SUMMARIZE_NODE_DESC',
    'MEMEX_RECORD_OUTCOME_DESC',
]
