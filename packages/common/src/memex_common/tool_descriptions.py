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
    'Record how previously retrieved memory units contributed to the outcome.\n'
    '\n'
    'Shape: units=[{unit_id: UUID, verb: "helpful"|"not_helpful"|"not_used", '
    'reason: str|null}].\n'
    'Required: `reason` for helpful and not_helpful; optional for not_used.\n'
    'Invalid: bare `success=True` (or any bare flag) without `units`. Server '
    'returns HTTP 400.\n'
    '\n'
    "When to call: the user signals an existing-memory fix held or didn't — "
    '"that worked", "it\'s holding", "record that as a successful resolution", '
    '"stop suggesting that". Search for the relevant unit(s), then stamp them. '
    'Do NOT capture confirmations as new notes — paired-write on existing '
    'units. Add a fresh memex_add_note only when the user is recording a new '
    'durable insight, not confirming a known one.\n'
    '\n'
    '<example>User: "The JWT rotation change we landed last sprint — it\'s '
    'been clean." WRONG: search returns the rotation-decision unit u1 → call '
    'memex_add_note(title="JWT rotation confirmed working"). RIGHT: same '
    'search → memex_record_outcome(units=[{unit_id:u1, verb:"helpful", '
    'reason:"new cadence held 30 days"}]).</example>'
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
    'Observations are read-only projections of memory units. Passing an '
    'observation UUID (`unit_metadata.virtual: true`) returns HTTP 400 with '
    'body `{source_memory_units: [...]}` — re-issue against one of the listed '
    'MU IDs to deprioritize the underlying fact. Observations are refreshed '
    'asynchronously on the surviving evidence (typically within seconds; rely '
    'on the next search to see the update).\n'
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
    "Error envelopes: rate-limit returns {error:'rate_limit_exceeded', "
    'retry_after_seconds}. Concurrent-refresh returns {error:'
    "'reflection_abandoned', retry_after_seconds, hint} — the fresh "
    'model is already persisted by another worker; prefer re-reading '
    'via memex_get_entity or memex_memory_search rather than retrying.\n'
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
    'periodic vault-wide maintenance.\n'
    '\n'
    'Returns ``abandoned: true`` when a concurrent worker refreshed the mental '
    'model between read and write; the fresh state is already persisted — '
    'prefer re-reading via memex_get_entity / memex_memory_search rather '
    'than retrying reconsolidate.'
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

MEMEX_KV_PUT_DESC = (
    'Put a namespaced operational pointer into KV — preference, project '
    'binding, or convention. NOT for content facts (those become memory '
    'units via memex_add_note) and NOT for how-to procedures (those go to '
    'the procedural plane via memex_case_submit).\n'
    '\n'
    'Required: value (str), key (str). Top-level prefixes: `global:`, '
    '`user:`, `project:<id>:`, `app:<app-id>:`. Invalid → HTTP 400.\n'
    '\n'
    'When to call: proactively on "remember…" / "I prefer…" / "we use…". '
    'Namespace by scope cue:\n'
    '- identity ("I prefer X", no qualifier) → `user:<field>`\n'
    '- "in this repo / project" → `project:<id>:<field>`\n'
    '- "whenever I use <app>" → `app:<app-id>:<field>` (NOT `user:<app>:…`)\n'
    '- "company-wide" → `global:<field>`\n'
    'Full rules in agent system prompt (§KV_NAMESPACE).\n'
    '\n'
    'Optional: ttl_seconds. Generates a semantic embedding for memex_kv_search.\n'
    '\n'
    'Deletion is CLI-only (`memex kv delete`).'
)


MEMEX_KV_GET_DESC = (
    'Get a KV entry by exact key.\n'
    '\n'
    'Required: key (str — must include namespace prefix).\n'
    '\n'
    'Returns 404 if the key does not exist. Use memex_kv_search for fuzzy lookup.'
)


MEMEX_KV_SEARCH_DESC = (
    'Semantic-search the KV store. Returns entries ranked by semantic similarity '
    'to the query against the embedded value strings.\n'
    '\n'
    'Required: query (str).\n'
    'Optional: top_k (default 10), prefix (e.g. "global:procedure:" to scope '
    'to learned how-tos).\n'
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


# ---------------------------------------------------------------------------
# Procedural plane. Identity-anchor doctrine (procedure/strategy + pin
# chain + status lifecycle + the cases-are-notes rule) lives in
# agent_surface.PROCEDURAL_PLANE; these descriptions are Tier 1a
# contract only. Cases are NOT plane entries — memex_case_submit files
# them as notes. There is NO briefing tool: pinned cards arrive inside
# the session briefing automatically.
# ---------------------------------------------------------------------------


# NOTE: There are deliberately NO agent-facing procedural WRITE
# descriptions (create/update/upsert/deprecate). Procedures and
# strategies are DERIVED from cases (design §5/§8/§9); the agent's only
# procedural write is memex_case_submit. Direct authoring/editing lives
# on the operator surfaces (CLI `memex procedural …`, the curation TUI)
# and the HTTP/client CRUD the derivation worker uses — none of which
# carry an agent-facing tool description.


MEMEX_PROCEDURAL_GET_DESC = (
    'Fetch a single entry by UUID. Optional vault_id (mismatch → 404). '
    'Returns the full entry or 404.'
)


MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC = (
    'Fetch a single entry by its (kind, scope, verb, context) identity '
    'anchor. Returns the entry or null — the "did we already learn this?" '
    'existence probe. 400 on shape mismatch (e.g. context supplied for a '
    'strategy — strategies anchor on scope+verb only).'
)


MEMEX_PROCEDURAL_SEARCH_DESC = (
    'Hybrid BM25 + vector search (RRF-merged) across the procedural '
    'plane. Required: query. Optional: kind, scope, status (default '
    '"published"), top_k (default 10), include_pin_chain + pin_contexts, '
    'bm25_weight. Returns hits with match provenance (bm25/vector/pin/rrf).'
)


MEMEX_CASE_SUBMIT_DESC = (
    'Submit a worked episode as a case — the intentional capture of "what '
    'happened" after a multi-step task, a diagnosed bug, or a resolved '
    'incident. Cases are NOTES in a hidden system vault, never procedural '
    'entries. This is the ONLY way to record a how-to / workflow / worked '
    'episode; NEVER also memex_add_note the same content (a how-to saved '
    'as a note is invisible to the procedural plane). Procedures are '
    'DERIVED from the cases you submit — there is no procedure-write tool.\n'
    '\n'
    'Required: title, trigger (what kicked the episode off), outcome '
    '("success"|"failure"|"mixed"). Recommended: situation, actions (ordered '
    'steps), lesson, project_id (provenance), case_of (UUID of the procedure '
    'you just enacted — supply it whenever you know; it skips the assignment '
    'judge).\n'
    '\n'
    'Without case_of the server judges which procedure the case instances; '
    'contested judgments land in the lint queue (result.assignment.mode='
    '"escalated" + finding_id) — resolve via lint tools or leave for review.'
)


# ---------------------------------------------------------------------------
# External lint proposals (closed action catalogue).
# ---------------------------------------------------------------------------

MEMEX_LIST_LINT_ACTIONS_DESC = (
    'List the closed catalogue of lint resolution actions (read-only).\n'
    '\n'
    'Each entry: id, name, description, applicable_target_types, reversible, '
    'params_schema (JSON schema for the action params; null when '
    'parameterless).\n'
    '\n'
    'When to use: before memex_submit_lint_proposal with a proposed_action — '
    'pick an action whose applicable_target_types contains your target_type '
    'and shape params against its params_schema. The catalogue is closed: '
    'actions cannot be registered at runtime; it only grows with releases.'
)


MEMEX_SUBMIT_LINT_PROPOSAL_DESC = (
    'Submit an externally-detected lint proposal for human review in the '
    'maintenance cockpit.\n'
    '\n'
    'Required: vault_id (UUID or name); rule_name (lowercase slug you own — '
    'internal rule names and the llm_ prefix are reserved → rejected); '
    'lint_type structural|quality|governance|schema|routing; target_type '
    "(e.g. 'note', 'memory_unit', 'entity', 'kv'); target_id; description "
    '(why it fired, ≤500 chars); suggested_action (free-text remediation '
    'summary).\n'
    'Optional: evidence dict (keys resolution / rule_metadata / '
    'proposed_action are server-owned → rejected); proposed_action='
    '{action_name, params} from the closed catalogue — must apply to '
    'target_type and pass its params schema or the item is rejected.\n'
    '\n'
    'Result status: created | deduplicated (an existing finding already '
    'covers it; its finding_id is returned — resubmitting is idempotent) | '
    'cooldown_suppressed (a human resolved this same finding recently; do '
    'NOT retry) | rejected (detail explains why).\n'
    '\n'
    'When to use: your detection logic flagged a construct (misrouted note, '
    'stale KV entry, duplicate entities). Submission only files a pending '
    'finding — nothing mutates until a human resolves it.'
)


__all__ = [
    'MEMEX_CASE_SUBMIT_DESC',
    'MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC',
    'MEMEX_PROCEDURAL_GET_DESC',
    'MEMEX_PROCEDURAL_SEARCH_DESC',
    'MEMEX_KV_GET_DESC',
    'MEMEX_KV_LIST_DESC',
    'MEMEX_KV_SEARCH_DESC',
    'MEMEX_KV_PUT_DESC',
    'MEMEX_LIST_LINT_ACTIONS_DESC',
    'MEMEX_MEMORY_CONSOLIDATE_DESC',
    'MEMEX_MEMORY_DEPRIORITIZE_DESC',
    'MEMEX_MEMORY_RECONSOLIDATE_DESC',
    'MEMEX_MEMORY_RESTORE_DESC',
    'MEMEX_MEMORY_SUMMARIZE_NODE_DESC',
    'MEMEX_RECORD_OUTCOME_DESC',
    'MEMEX_SUBMIT_LINT_PROPOSAL_DESC',
]
