"""Canonical agent-surface text shared across MCP / Hermes / Claude Code.

This module is the **single source of truth** for Tier 1b universal system-
prompt content — the load-bearing agent doctrine every Memex-aware agent
must internalise. Agent-specific framing (e.g. Hermes session-note wiring,
Claude Code `author: "claude-code"` capture) lives in the per-agent surface
and is layered ON TOP of `compose_universal()` output.

See `CLAUDE.md` §"Agent-surface architecture" for the three-tier model.

Style discipline (calibrated from Anthropic + indiehackers + Claude Code
best practices):
1. Concrete, imperative. Bare imperatives beat descriptive prose.
2. Schema-shaped where structure helps; flat markdown otherwise.
3. Bidirectional constraints — pair every prohibition with the right alternative.
4. Keep rationale that aids generalization; drop pure justification.
5. U-shaped composition — load-bearing constraints at top AND bottom.
6. Deterministic output — no time/UUID/env in compose_universal(); cacheable prefix.

Exports:
- ``LAYER_ROUTING_PRIMER_*`` — 4-layer memory routing (existing).
- ``STORAGE_MODEL``, ``RETRIEVAL_ROUTING``, ``RESOLUTION_FLOW``, ``AXES``,
  ``HISTORICAL_ROUTING``, ``VIRTUAL_UNIT``, ``KV_NAMESPACE``, ``CITATIONS``
  — universal sections.
- ``compose_universal()`` — deterministic concatenation of universal sections
  in canonical U-shaped order.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 4-layer memory routing — single bullet-list form (PROSE was redundant).
# ---------------------------------------------------------------------------

LAYER_ROUTING_PRIMER_TABLE = """### Memory layers and tool routing

Memex stores four memory layers. Pick the right tool for the layer you need:

| Layer | What it stores | Retrieve with | Tiny example |
|---|---|---|---|
| **Episodic** ("what happened, when") | Timestamped, source-attributed Notes — sessions, reflections, decisions | `memex_note_search` / `memex_recent_notes` / `memex_find_note` | "Find yesterday's reflection about the deploy regression" |
| **Semantic** ("decontextualised facts") | MemoryUnits — short fact/observation/event statements extracted from notes | `memex_memory_search` / `memex_get_memory_units` / `memex_get_entity_mentions` | "What does v2 use for auth?" |
| **Conceptual** ("synthesised mental models") | MentalModels — reflection output bundling per-entity observations with trend tracking (new/strengthening/stable/weakening/stale) | `memex_survey` / `memex_get_entities` (with `mental_models=True`) | "What do you know about Project X overall?" |
| **Procedural** ("how to do X") | Procedures + strategies on the procedural plane — DERIVED from the cases you submit (there is no procedure-write tool) | `memex_procedural_search` (read); the only write is `memex_case_submit` (a worked episode) | "How do we cut a release?" |

**Rule of thumb.** Content-shaped question ("what about X?") → `memex_memory_search`;
source-shaped ("show me the notes about X") → `memex_note_search`; how-to ("how do
we X?") → `memex_procedural_search`. Never look up a how-to in KV — there is no KV
`procedure:` namespace; the procedural plane is its only home."""


LAYER_ROUTING_PRIMER_FRAGMENT = (
    'Memex memory layers (route by query type):\n'
    '\n'
    '  - Episodic ("what happened, when") → memex_note_search /\n'
    '    memex_recent_notes / memex_find_note. Source: timestamped Notes.\n'
    '  - Semantic ("decontextualised facts") → memex_memory_search /\n'
    '    memex_get_memory_units / memex_get_entity_mentions. Source: MemoryUnits.\n'
    '  - Conceptual ("synthesised mental models") → memex_survey /\n'
    '    memex_get_entities(mental_models=True). Source: MentalModels.\n'
    '  - Procedural ("how to do X") → memex_procedural_search (read); the only\n'
    '    write is memex_case_submit. Source: procedural plane (derived from cases).\n'
    '\n'
    'Default: memex_memory_search for content, memex_note_search for source,\n'
    'memex_procedural_search for how-to. There is no KV `procedure:` namespace.'
)


# ---------------------------------------------------------------------------
# Tier 1b universal sections — added 2026-05-14.
# Each section is LLM-optimized. Bare imperatives. Rationale-that-aids-
# generalization only. No philosophical framing.
# ---------------------------------------------------------------------------

CRITICAL_HEADER = """## Critical constraints

<critical_constraint name="record_outcome_shape">
`memex_record_outcome` requires `units=[{unit_id, verb, reason}]`. Bare `success=True` → HTTP 400.
</critical_constraint>

<critical_constraint name="observation_read_only">
Observations (`unit_metadata.virtual: true`) are read-only projections of MUs; `memex_memory_deprioritize` on an observation UUID returns HTTP 400 with `source_memory_units`. Re-issue against one of the listed MU IDs.
</critical_constraint>

<critical_constraint name="kv_scope_qualifier">
KV namespace = scope qualifier (NOT grammatical person). "I prefer X for this project" → `project:<id>:` not `user:`.
</critical_constraint>

<critical_constraint name="citations_required">
Cite every load-bearing claim grounded in Memex content. Never fabricate titles/ids.
</critical_constraint>"""


STORAGE_MODEL = """## Storage layers

- **Notes** — markdown source. `memex_add_note` for first capture; `memex_append_note(note_key, delta)` to extend (never re-ingest whole body).
- **Memory units** — append-only facts extracted from notes. NEVER edit/replace/delete. To record a change, ingest a new note; contradiction detection runs at extraction.
- **KV store** — namespaced operational state. Mutable upsert by key.

Reflection produces per-entity mental models (read-only — surface via `memex_memory_search` / `memex_survey`)."""


RETRIEVAL_ROUTING = """## Retrieval routing

Match the query shape; call the listed tool(s):

- **Title fragment** → `memex_find_note` → `memex_get_page_indices` + `memex_get_nodes`.
- **Relationships** → `memex_list_entities` → `memex_get_entity_cooccurrences` → `memex_get_entity_mentions`.
- **Specific fact / single question** → `memex_memory_search` AND `memex_note_search` in parallel. Retry `expand_query=true` if insufficient.
- **Comprehensive view of a topic/entity** ("everything/overview/tell me all about X") → `memex_survey(query)` FIRST, OR ≥3 facet-scoped `memex_memory_search` calls. One search result is NEVER enough. <example>"Tell me everything about Topic-X" → WRONG: one `memex_memory_search("Topic-X")`. RIGHT: `memex_survey("Topic-X")`, or 3 facet-scoped `memex_memory_search` → consolidate.</example>
- **Broad/panoramic** (vault-wide, no topic) → `memex_get_vault_summary` first; escalate to `memex_survey(query)` if too coarse.
- **KV** ("what's our X?" / "what convention?" / "what do I prefer?" / "what setting?") → `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()` FIRST. NEVER `ls`/`Glob`/`Read`/`Bash`/inspect local files first — preferences/conventions/settings live in KV, not on disk. Wake words route verbatim: `KV: get <key>`, `KV: search <query>`, `Store in KV: <key>=<value>`. (How-to procedures are NOT KV → procedural plane.)

After `memory_search`: call `memex_get_notes_metadata`. After `note_search`: metadata is inline — do NOT call `memex_get_notes_metadata`. `memex_read_note` only when `total_tokens < 500`.

For list-shape browse tools (`memex_recent_notes`, `memex_list_notes`, `memex_list_entities`), pass `slim=True` when you need only IDs/titles/timestamps — drops summaries + descriptions to fit tool-output caps. Default `slim=False`."""


SEARCH_QUERIES = """## Search query formulation

<critical_constraint name="search-queries">
ALWAYS formulate search queries as natural language, NEVER as keyword lists.
ALWAYS preserve proper nouns, amounts, dates, qualifiers from the original question.
ALWAYS search for the subject/activity, NOT the answer type.
</critical_constraint>"""


RESOLUTION_FLOW = """## 5-step resolution flow

<critical_constraint name="outcome_routing">
Triggers: success — "that worked", "that fixed it", "yes, that did it", "perfect", "record it as a success", "save this approach"; failure — "stop suggesting X", "didn't work", "we removed it", "that was wrong", "drop that idea". These ALWAYS route to `memex_record_outcome` on EXISTING units; NEVER to `memex_add_note`. The outcome is a counter increment on the unit's Memory Worth — a new note describing the success is NOT detected as an outcome.
</critical_constraint>

<example>"That fixed it, record it as a success." → WRONG: `memex_add_note(title="Resolution: X worked")`. RIGHT: `memex_memory_search` for candidate units → READ bodies → `memex_record_outcome(units=[{unit_id, verb:"helpful", reason}])`.</example>

1. **Disambiguate** — ambiguous scope (multiple candidates, no temporal anchor)? ASK before writing.
2. **Route** — title → `memex_find_note`; content → `memex_memory_search`. Pick one:
   - A entity-anchored: `memex_list_entities` → `memex_get_entity_mentions`.
   - B cross-note: `memex_memory_search(top_k=30)`. `top_k` must be ≥30.
   - C single-note: `memex_get_page_indices` → `memex_get_memory_units(chunk_ids=…)`.
3. **Judge** — READ unit bodies; pick outcome-relevant subset. NEVER bulk-write.
4. **+5. Paired writes** on the judged subset:
   - Success → `memex_record_outcome(units=[{unit_id, verb:"helpful", reason}])`. No deprio.
   - Failure → `memex_record_outcome(units=[{unit_id, verb:"not_helpful", reason}])` AND `memex_memory_deprioritize(unit_id, reason)`. SAME subset."""


AXES = """## Orthogonal axes

- `memex_record_outcome` = MW gradient (append-only; not reversible).
- `memex_memory_deprioritize` = binary surface state (reversible via `memex_memory_restore`).

User-confirmed-fix stamps BOTH."""


HISTORICAL_ROUTING = """## Historical / audit routing

Triggers: "evolved", "used to", "history of", "what changed", "audit".

- Specific unit → `memex_get_unit_history(unit_id)`.
- Broad audit → `memex_memory_search(apply_pre_filter=False)` (bypasses MW/FSFM/confidence filters)."""


VIRTUAL_UNIT = """## Read-only observations

<critical_constraint name="virtual_unit_filter">
Mental-model observations are read-only projections of memory units (`unit_metadata.virtual: true`). `memex_memory_deprioritize` on an observation UUID returns HTTP 400 with `{source_memory_units: [...]}`; re-issue against one of those MU IDs to suppress the underlying fact. Observations refresh asynchronously on the surviving evidence.

An observation's `evidence` may include STALE memory units (superseded by a newer contradicting note). STALE evidence stays cited as historical support and is NOT auto-pruned — treat it as audit-trail, not an active claim.
</critical_constraint>"""


KV_NAMESPACE = """## Preferences / conventions → `memex_kv_put`, NOT local files

<critical_constraint name="kv_routing">
"remember"/"save"/"for future sessions"/"going forward" directives conveying a preference, convention, or setting → `memex_kv_put`. Do NOT write to local files (CLAUDE.md, AGENTS.md, .memex/), do NOT use `memex_add_note`, do NOT just acknowledge.
</critical_constraint>

Pick the namespace by scope cue (NOT grammatical person). `app:`/`project:`/`global:` ALL override `user:` when their cue is present; default `user:` only when NO other cue applies.

| Scope cue | Namespace |
|---|---|
| identity ("about me", "I prefer X") | `user:` |
| "this repo/project", "in this codebase" | `project:<id>:` |
| "company-wide", "we standardise on" | `global:` |
| "when I use <app>", "in Claude Code/Hermes" | `app:<app-id>:` |

<critical_constraint name="kv_vs_procedural">
KV holds ONE static binding — a PREFERENCE / SETTING / CONVENTION ("Python 3.12", "dark theme", "lint before commit"). A multi-step WORKFLOW you'd reuse and search ("how we deploy", "release steps") is NOT KV → procedural plane (`memex_procedural_search` to recall, `memex_case_submit` to write). No KV `procedure:` namespace — the plane is its only home.
</critical_constraint>

Ambiguous? ASK before writing.

<example>"I prefer Neovim" → `user:editor`</example>
<example>"For this project: Python 3.10" → `project:<id>:lang:python`</example>
<example>"Company-wide: Python 3.12 min" → `global:lang:python:min`</example>
<example>"When I use Claude Code: dark theme" → `app:claude-code:theme` (<app> cue beats "I"/"my")</example>
<example>"Always lint before commit" → `global:lint:commit` (one-line convention, not a workflow)</example>
<example>"How we deploy: check status, verify secrets, push, health-check" → procedural plane, NOT KV</example>"""


PROCEDURAL_PLANE = """## Procedural plane — how-to memory

How-to memory (workflows/strategies/worked episodes) is a SEPARATE plane from semantic memory (facts/notes). Route every recall and every write to exactly ONE plane.

<critical_constraint name="procedural_vs_semantic_search">
Recall HOW to do something ("how we deploy", "the release steps") → `memex_procedural_search` ONLY; `memex_memory_search`/`memex_note_search` search the semantic plane and return NO procedures. Recall a FACT / "what is X" / a document → `memex_memory_search`/`memex_note_search` ONLY; never `memex_procedural_search`.
</critical_constraint>

<critical_constraint name="procedural_retrieve_first">
The MOMENT a request is an action you might have a procedure for — deploy, release, cut/ship a build, bump a version, rotate creds, run a migration, set up an env — your FIRST tool call is `memex_procedural_search(query="<the task>")`, BEFORE you act, narrate steps, read files, or run a shell/terminal command. Do not improvise from the filesystem or memory until you have checked the plane. A hit is a learned procedure: follow it, don't re-derive; do not also semantic-search it.
</critical_constraint>

<critical_constraint name="procedural_vs_semantic_add">
Record = exactly ONE write to exactly ONE plane:
- reusable WORKFLOW or WORKED EPISODE ("I did task X, here's how it went"; Trigger/Situation/Actions/Outcome) → `memex_case_submit` and NOTHING ELSE. NEVER also `memex_add_note` (a how-to saved as a note is invisible to the procedural plane — the #1 mistake); never instead of it. Pass `case_of=<id>` when you followed a known procedure.
- FACT / DECISION / DOCUMENT ("what is true") → `memex_add_note` ONLY; never `memex_case_submit`.
There is NO procedure create/update tool — procedures and strategies are DERIVED from the cases you submit. You READ them; the system writes them.
</critical_constraint>

<critical_constraint name="close_the_loop">
After you ENACT a known procedure (pass `case_of=<id>`), or whenever the user asks to "record" / "log how it went" / "make a record" of a run, you MUST end by calling `memex_case_submit` (set `outcome`) — searching/doing is only HALF the loop. For any OTHER task, apply the capture test: file a case only if you'd want these steps back next time; routine work gets nothing.
</critical_constraint>

Two derived kinds, identity anchor `(kind, scope, verb, context)`:
- `procedure` — a workflow; keyed by `verb`+`context` (e.g. verb=`deploy`, context=`nomad`).
- `strategy` — a heuristic over the procedures sharing its `(scope, verb)`; `context` FORBIDDEN.
Search matches `trigger` (when-to-use). Scope: `global` | `project:<id>` | `app:<id>` (no user scope).

Cases are NOTES (role=`case`), filed by `memex_case_submit` in the hidden case vault to feed derivation. Pinned procedures arrive in your session briefing automatically. `memex_procedural_search` defaults to `status="published"`."""


CITATIONS = """## Citations

Cite source notes inline for every claim grounded in Memex content: `…claim [note-title-or-id].`

One reference per load-bearing claim. Never fabricate titles or ids — say "I cannot identify a specific source" instead."""


CRITICAL_FOOTER = """## Critical reminders

<critical_reminder name="record_outcome_shape">
`memex_record_outcome`: `units=[{unit_id, verb, reason}]`. Bare `success=True` → 400.
</critical_reminder>

<critical_reminder name="virtual_unit_filter">
Observations (`unit_metadata.virtual: true`) → deprio returns 400 with `source_memory_units`; re-issue against one of the listed MU IDs.
</critical_reminder>

<critical_reminder name="kv_scope_qualifier">
KV namespace: scope qualifier picks the namespace. "for this project" → `project:<id>:` even with "I"/"my".
</critical_reminder>

<critical_reminder name="citations_required">
Cite inline; never fabricate.
</critical_reminder>"""


# ---------------------------------------------------------------------------
# Tier 1a — MCP transport instructions (SSOT).
#
# Imported by the MCP server (``server.py`` ``instructions=`` field) AND by
# the CLI ``agent-surface mcp`` target. Same bytes in both surfaces; pinned
# by identity tests so a divergent edit cannot ship.
# ---------------------------------------------------------------------------


MCP_TRANSPORT_INSTRUCTIONS = """Memex MCP — personal knowledge management.

TOOL DISCOVERY (progressive disclosure)
- If only memex_tags/memex_search/memex_get_schema are visible, run:
    memex_tags() → memex_search(query, tags=[...]) → memex_get_schema(tools=[...])
- Call any tool by name directly if you already know it.

VAULT DEFAULTS — vault params are optional. Writes default to the active
vault; reads to the search vaults (.memex.yaml or global config). Pass
vault_id/vault_ids to override. "*" expands to content vaults only; system
vaults (e.g. inbox) join only when named or via include_system_vaults=true
(an unconditional union — adds every system vault regardless of "*" or
named vaults). To scope to one system vault, name it and omit the flag.

NEVER fabricate IDs — use only IDs returned by a tool.

This surface is the tool protocol ONLY. Retrieval routing, storage model,
resolution flow, KV namespace rules, virtual-unit warning, and citation
discipline live in the system prompt: compose them from
`memex_common.agent_surface.compose_universal()` — or `compose_with_procedural()`
if you mount the procedural tools (`memex_procedural_*` / `memex_case_submit`) —
(Python), or `memex agent-surface universal` / `... claude-code` (shell), when
building a Memex-aware agent beyond raw MCP."""


# ---------------------------------------------------------------------------
# Composer — deterministic universal block (Tier 1b).
# ---------------------------------------------------------------------------


def compose_universal() -> str:
    """Return the universal Tier 1b system-prompt block, in canonical U-shaped order.

    Identical bytes on every call (deterministic). No time, UUID, or env state.
    Suitable for cacheable prompt prefix (per dbreunig's Claude Code cache-
    boundary finding).

    Composition order (primacy → middle → recency):
        CRITICAL_HEADER → STORAGE_MODEL → RETRIEVAL_ROUTING →
        SEARCH_QUERIES → RESOLUTION_FLOW → AXES → HISTORICAL_ROUTING →
        VIRTUAL_UNIT → KV_NAMESPACE → CITATIONS → CRITICAL_FOOTER

    ``LAYER_ROUTING_PRIMER_TABLE`` is NOT included by default — it overlaps
    with ``RETRIEVAL_ROUTING`` (by-query-type vs by-layer decomposition of
    the same routing decisions). Agents that want the 4-layer table can
    import and append it separately.
    """
    return '\n\n'.join(
        [
            CRITICAL_HEADER,
            STORAGE_MODEL,
            RETRIEVAL_ROUTING,
            SEARCH_QUERIES,
            RESOLUTION_FLOW,
            AXES,
            HISTORICAL_ROUTING,
            VIRTUAL_UNIT,
            KV_NAMESPACE,
            CITATIONS,
            CRITICAL_FOOTER,
        ]
    )


def compose_with_procedural() -> str:
    """Tier 1b universal block + the procedural-plane doctrine.

    Opt-in (not in ``compose_universal``) because the procedural tools
    (``memex_procedural_search`` / ``_get`` / ``_get_by_identity`` +
    ``memex_case_submit``) are only mounted by clients that ship the
    procedural plane — including
    PROCEDURAL_PLANE in the default universal would burn ~1,750 chars on
    agents that have no procedural tools. Hermes briefing and the Claude
    Code SessionStart hook append PROCEDURAL_PLANE on top of
    ``compose_universal()`` when the procedural plane is active.
    """
    return compose_universal() + '\n\n' + PROCEDURAL_PLANE


__all__ = [
    # Existing 4-layer routing primer (Tier 1b component).
    'LAYER_ROUTING_PRIMER_FRAGMENT',
    'LAYER_ROUTING_PRIMER_TABLE',
    # Tier 1a SSOT (MCP transport instructions).
    'MCP_TRANSPORT_INSTRUCTIONS',
    # New universal sections.
    'AXES',
    'CITATIONS',
    'CRITICAL_FOOTER',
    'CRITICAL_HEADER',
    'PROCEDURAL_PLANE',
    'HISTORICAL_ROUTING',
    'KV_NAMESPACE',
    'RESOLUTION_FLOW',
    'RETRIEVAL_ROUTING',
    'SEARCH_QUERIES',
    'STORAGE_MODEL',
    'VIRTUAL_UNIT',
    # Composer.
    'compose_universal',
    'compose_with_procedural',
]
