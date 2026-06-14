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
    'Call when the user confirms an EXISTING memory held or failed — "that '
    'worked", "it\'s holding", "stop suggesting that". Search for the unit(s), '
    'then stamp them. NOT for new insights: a fresh durable claim is a '
    'memex_add_note, not an outcome on a known one.\n'
    '\n'
    'Required: units=[{unit_id: UUID, verb: "helpful"|"not_helpful"|"not_used", '
    'reason: str|null}]; reason is required for helpful/not_helpful, optional '
    'for not_used.\n'
    'HTTP 400: bare `success=True` (or any bare flag) without `units`.\n'
    '\n'
    '<example>User: "The JWT rotation we landed last sprint has been clean." '
    'WRONG: memex_add_note(title="JWT rotation confirmed working"). RIGHT: '
    'memex_record_outcome(units=[{unit_id:u1, verb:"helpful", reason:"new '
    'cadence held 30 days"}]).</example>'
)


MEMEX_MEMORY_DEPRIORITIZE_DESC = (
    "Call to lower a memory unit's retrieval rank when it is misleading, "
    'outdated, or noise contaminating results — without deleting it '
    '(NON-DESTRUCTIVE). Prefer this over archive (CLI-only, destructive) '
    'unless the unit must leave the entity graph entirely.\n'
    '\n'
    'Required: unit_id (UUID), reason (free text, logged to maintenance ledger).\n'
    'Reversible via memex_memory_restore.\n'
    '\n'
    'Pair with memex_record_outcome(units=[{verb:"not_helpful", reason}]) when '
    'the user says the memory was wrong — different axes (record_outcome = '
    'append-only MW counter; deprioritize = binary surface state).\n'
    '\n'
    'Observation UUIDs (`unit_metadata.virtual: true`) → HTTP 400 with body '
    '`{source_memory_units: [...]}`; re-issue against a listed MU ID to '
    'deprioritize the underlying fact. Observations refresh asynchronously on '
    'the surviving evidence — rely on the next search to see the update.'
)


MEMEX_MEMORY_RESTORE_DESC = (
    'Call to undo memex_memory_deprioritize: flip `is_deprioritized` to false '
    'so the unit re-enters default-scope retrieval. Writes an audit_logs row.\n'
    '\n'
    'Required: unit_id (UUID).'
)


# ---------------------------------------------------------------------------
# Reflection / consolidation.
# ---------------------------------------------------------------------------

MEMEX_MEMORY_SUMMARIZE_NODE_DESC = (
    'Call to trigger reflection SYNCHRONOUSLY when retrieved facts about a '
    'topic conflict, are incomplete, or are scattered and you need a coherent '
    'mental model before continuing — the synchronous counterpart to the '
    'background reflect loop. Use sparingly (LLM-intensive); default to '
    'background reflection unless you have an in-session reason to run now.\n'
    '\n'
    'Params: entity_id (one entity — preferred) OR note_ids (a specific set). '
    'scope="incremental" (default — only new evidence) or "full" (re-evaluate '
    'all, capped at 1000 units).\n'
    'Returns: ReflectionResult with updated/new MentalModel(s).\n'
    '\n'
    'Rate-limited per (entity, vault). Error envelopes carry retry_after_seconds '
    "— honor it, do not retry-loop. {error:'rate_limit_exceeded', "
    "retry_after_seconds}; {error:'reflection_abandoned', retry_after_seconds, "
    'hint} means another worker already persisted the fresh model — re-read via '
    'memex_get_entity or memex_memory_search instead of retrying.'
)


MEMEX_MEMORY_RECONSOLIDATE_DESC = (
    'Call on CONCRETE contradiction signals for ONE entity (e.g. two memory '
    'units about Project X with opposite claims), or after ingesting a note '
    'that supersedes earlier claims: re-runs contradiction detection across its '
    'units and refreshes its mental model. Entity-scoped.\n'
    '\n'
    'NOT for periodic maintenance — use memex_memory_consolidate (vault-scoped: '
    'batch-deprioritizes low-MW + stale units, writes the maintenance ledger).\n'
    '\n'
    'Required: entity_id, vault_id.\n'
    '\n'
    'Returns ``abandoned: true`` when a concurrent worker refreshed the model '
    'between read and write — the fresh state is already persisted; re-read via '
    'memex_get_entity / memex_memory_search instead of retrying.'
)


MEMEX_MEMORY_CONSOLIDATE_DESC = (
    'Call for periodic vault-wide maintenance: batch-deprioritize low-MW + '
    'stale units, write a maintenance ledger entry. NOT for in-session conflict '
    'resolution — use memex_memory_reconsolidate (entity-scoped contradiction '
    'handling).\n'
    '\n'
    'Required: vault_id. Optional: dry_run=true to preview without applying.\n'
    'Rate-limited; honor `retry_after_seconds` on the response if rejected.'
)


# ---------------------------------------------------------------------------
# KV store.
# ---------------------------------------------------------------------------

MEMEX_KV_PUT_DESC = (
    'Call proactively on "remember…" / "I prefer…" / "we use…" to store a '
    'namespaced operational pointer — preference, project binding, or '
    'convention. NOT for content facts (those become memory units via '
    'memex_add_note); NOT for how-to procedures (those go to the procedural '
    'plane via memex_case_submit).\n'
    '\n'
    'Required: value (str), key (str). Key MUST start with a top-level prefix '
    'or → HTTP 400. Pick the prefix by scope cue:\n'
    '- identity ("I prefer X", no qualifier) → `user:<field>`\n'
    '- "in this repo / project" → `project:<id>:<field>`\n'
    '- "whenever I use <app>" → `app:<app-id>:<field>` (NOT `user:<app>:…`)\n'
    '- "company-wide" → `global:<field>`\n'
    'Full rules in agent system prompt (§KV_NAMESPACE).\n'
    '\n'
    'Optional: ttl_seconds. Generates a semantic embedding for memex_kv_search. '
    'Deletion is CLI-only (`memex kv delete`).'
)


MEMEX_KV_GET_DESC = (
    'Call when you know the EXACT key. Returns the entry, or 404 if absent — '
    'use memex_kv_search for fuzzy/semantic lookup.\n'
    '\n'
    'Required: key (str — must include namespace prefix).'
)


MEMEX_KV_SEARCH_DESC = (
    'Call when you do NOT know the exact key: semantic search ranks entries by '
    'similarity to the query against the embedded value strings. For exact-key '
    'lookup use memex_kv_get.\n'
    '\n'
    'Required: query (str).\n'
    'Optional: top_k (default 10), prefix to scope to a namespace (e.g. "user:", '
    '"project:<id>:"). KV holds preferences/settings/conventions — NOT how-tos; '
    'recall a how-to with memex_procedural_search.'
)


MEMEX_KV_LIST_DESC = (
    'Call to enumerate KV entries by namespace (no ranking). For semantic '
    'relevance use memex_kv_search instead.\n'
    '\n'
    'Optional: prefix (e.g. "user:", "project:<id>:"), limit (default 100), '
    'offset (pagination).\n'
    'Returns key+value+scope per entry; values are NOT semantically reranked.'
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
    'Call when you have the entry UUID. Returns the full procedure/strategy, '
    'or 404 (also on vault_id mismatch). To find one by query use '
    'memex_procedural_search.\n'
    '\n'
    'Required: id (UUID). Optional: vault_id.'
)


MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC = (
    'Call as the "did we already learn this?" existence probe: fetch by '
    '(kind, scope, verb, context) identity anchor. Returns the entry or null. '
    '400 on shape mismatch (e.g. context supplied for a strategy — strategies '
    'anchor on scope+verb only).\n'
    '\n'
    'Required: kind, scope, verb — plus context for a procedure '
    '(REQUIRED for procedures, FORBIDDEN for strategies).'
)


MEMEX_PROCEDURAL_SEARCH_DESC = (
    'Call FIRST — before you act, narrate steps, read files, or run a shell '
    'command — to find a how-to / workflow / strategy for a task you may have '
    'done before (deploy, release, cut/ship a build, bump a version, rotate '
    'creds, migrate): hybrid '
    'BM25 + vector search (RRF-merged) across the procedural plane. Use '
    'memex_procedural_get when you already have the UUID.\n'
    '\n'
    'Required: query. Optional: kind, scope, status (default "published"), '
    'limit (default 10), include_pin_chain + pin_contexts, bm25_weight.\n'
    'Returns hits with match provenance (bm25/vector/pin/rrf).'
)


MEMEX_CASE_SUBMIT_DESC = (
    'Call after a multi-step task, diagnosed bug, or resolved incident to '
    'capture "what happened" as a case. This is the ONLY way to record a '
    'how-to / workflow / worked episode. NEVER also memex_add_note the same '
    'content — a how-to saved as a note is invisible to the procedural plane. '
    'Cases are NOTES in a hidden system vault, never procedural entries; '
    'procedures are DERIVED from the cases you submit (there is no '
    'procedure-write tool). CLOSE THE LOOP: call this after you enact a '
    'procedure or whenever asked to record / log / note a run — do not end '
    'the turn without it.\n'
    '\n'
    'Required: title, trigger (what kicked it off), outcome '
    '("success"|"failure"|"mixed"). Recommended: situation, actions (ordered '
    'steps), lesson, project_id, case_of (UUID of the procedure you enacted — '
    'supply when known; it skips the assignment judge).\n'
    '\n'
    'Without case_of the server judges which procedure the case instances; '
    'contested judgments land in the lint queue (result.assignment.mode='
    '"escalated" + finding_id) — resolve via lint tools or leave for review.'
)


# ---------------------------------------------------------------------------
# External lint proposals (closed action catalogue).
# ---------------------------------------------------------------------------

MEMEX_LIST_LINT_ACTIONS_DESC = (
    'Call before memex_submit_lint_proposal with a proposed_action: lists the '
    'closed, read-only catalogue of lint resolution actions. Pick one whose '
    'applicable_target_types contains your target_type and shape params against '
    'its params_schema.\n'
    '\n'
    'Each entry: id, name, description, applicable_target_types, reversible, '
    'params_schema (JSON schema for the params; null when parameterless).\n'
    'The catalogue is closed — actions cannot be registered at runtime; it only '
    'grows with releases.'
)


MEMEX_SUBMIT_LINT_PROPOSAL_DESC = (
    'Call when your detection logic flags a construct (misrouted note, stale KV '
    'entry, duplicate entities) to file it for human review. Submission only '
    'creates a PENDING finding — nothing mutates until a human resolves it.\n'
    '\n'
    'Required: vault_id (UUID or name); rule_name (lowercase slug you own — '
    'internal rule names and the llm_ prefix are reserved → rejected); '
    'lint_type structural|quality|governance|schema|routing; target_type '
    "(e.g. 'note', 'memory_unit', 'entity', 'kv'); target_id; description "
    '(why it fired, ≤500 chars); suggested_action (free-text remediation).\n'
    'Optional: evidence dict (keys resolution / rule_metadata / proposed_action '
    'are server-owned → rejected); proposed_action={action_name, params} from '
    'the closed catalogue — must apply to target_type and pass its params '
    'schema or → rejected.\n'
    '\n'
    'Result status: created | deduplicated (existing finding covers it; '
    'finding_id returned; resubmitting is idempotent) | cooldown_suppressed (a '
    'human resolved this recently — do NOT retry) | rejected (detail says why).'
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
