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
| **Procedural-observations** ("adaptations to context") | KV entries under `procedure:<verb>:<context-tag>` — observations about how to adapt your existing skills to a context, NOT the procedures themselves | `memex_kv_search` / `memex_kv_get` with `prefix='procedure:'` | "For this user, `deploy` means staging — never prod after 6pm" |

**Rule of thumb.** If unsure, default to `memex_memory_search` for content-shaped
questions ("what about X?") and `memex_note_search` for source-shaped questions
("show me the notes about X"). Agents own the verb (the executable how-to);
Memex owns the adverb (observations about how to adapt it). Core / Cross-Context
layers are informational only — not first-class in Memex today."""


LAYER_ROUTING_PRIMER_FRAGMENT = (
    'Memex memory layers (route by query type):\n'
    '\n'
    '  - Episodic ("what happened, when") → memex_note_search /\n'
    '    memex_recent_notes / memex_find_note. Source: timestamped Notes.\n'
    '  - Semantic ("decontextualised facts") → memex_memory_search /\n'
    '    memex_get_memory_units / memex_get_entity_mentions. Source: MemoryUnits.\n'
    '  - Conceptual ("synthesised mental models") → memex_survey /\n'
    '    memex_get_entities(mental_models=True). Source: MentalModels.\n'
    '  - Procedural-observations ("adaptations to context") → memex_kv_search /\n'
    "    memex_kv_get(prefix='procedure:'). Source: KV `procedure:<verb>:<tag>`.\n"
    '\n'
    'Default: memex_memory_search for content-shaped questions; memex_note_search\n'
    'for source-shaped questions. The agent owns the verb; Memex owns the adverb.'
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

<critical_constraint name="virtual_unit_404">
Virtual units (`unit_metadata.virtual: true`) → `memex_memory_deprioritize` returns 404. Filter before paired writes.
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

- **Title fragment** → `memex_find_note` → `memex_get_page_indices` + `memex_get_nodes`.
- **Relationships** → `memex_list_entities` → `memex_get_entity_cooccurrences` → `memex_get_entity_mentions`.
- **Content lookup** → `memex_memory_search` AND `memex_note_search` in parallel. Retry `expand_query=true` if insufficient.
- **Broad/panoramic** → `memex_get_vault_summary` first; escalate to `memex_survey(query)` if too coarse.
- **KV** → `memex_kv_get(key)` exact / `memex_kv_search(query)` fuzzy / `memex_kv_list()`.

After `memory_search`: call `memex_get_notes_metadata`. After `note_search`: metadata inline — do NOT call `memex_get_notes_metadata` again. `memex_read_note` only when `total_tokens < 500`."""


RESOLUTION_FLOW = """## 5-step resolution flow

Triggers: "that worked"/"lock it in" (success); "stop suggesting X"/"didn't work" (failure).

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


VIRTUAL_UNIT = """## Virtual units (cannot be deprioritized)

<critical_constraint name="virtual_unit_filter">
`unit_metadata.virtual: true` units are synthesized from MentalModel observations — no DB row. `memex_memory_deprioritize` on their UUID returns HTTP 404.

Filter candidates to `unit_metadata.virtual` unset/false BEFORE paired writes. If empty, fall back to entity-anchored search to recover real source units.
</critical_constraint>"""


KV_NAMESPACE = """## KV namespace by scope qualifier

The scope qualifier picks the namespace, NOT the grammatical person.

- No scope, identity-shaped ("Remember about me: I prefer Neovim") → `user:` (e.g. `user:editor`).
- "this repo"/"this project"/"on <named project>" → `project:<id>:`. **Wins even when request opens with "I" or "my"**: "My preference for this project is Python 3.10" → `project:<id>:lang:python`, NOT `user:lang`.
- "across our projects"/"we standardise on" → `global:`.
- "in <app-name>" → `app:<app-id>:`.
- Learned how-tos → `procedure:<verb>:<context-tag>`; pair with `memex_record_outcome(target_type="kv_key", kv_key=…)`.

Narrower scope wins (project beats user). Ambiguous? ASK before writing.

<example>"I prefer Neovim" → memex_kv_write(value="Neovim", key="user:editor")</example>
<example>"My preference for this project is Python 3.10" → memex_kv_write(value="3.10", key="project:<repo-id>:lang:python")</example>"""


CITATIONS = """## Citations

Cite source notes inline for every claim grounded in Memex content: `…claim [note-title-or-id].`

One reference per load-bearing claim. Never fabricate titles or ids — say "I cannot identify a specific source" instead."""


CRITICAL_FOOTER = """## Critical reminders

<critical_reminder name="record_outcome_shape">
`memex_record_outcome`: `units=[{unit_id, verb, reason}]`. Bare `success=True` → 400.
</critical_reminder>

<critical_reminder name="virtual_unit_filter">
Virtual units (`unit_metadata.virtual: true`) → deprio returns 404; filter them.
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
- If only memex_tags/memex_search/memex_get_schema appear, run:
    memex_tags() → memex_search(query, tags=[...]) → memex_get_schema(tools=[...])
- You can also call tools by name if you already know them.

VAULT DEFAULTS — vault parameters are optional. Writes default to the
active vault; reads default to search vaults (from .memex.yaml or global
config). Pass vault_id/vault_ids to override.

NEVER fabricate IDs. Use only IDs returned from tool output.

This MCP surface is the tool protocol only. Full retrieval routing,
storage model, 5-step resolution flow, KV namespace rules, virtual-unit
warning, and citation discipline live in the agent's system prompt —
compose them from `memex_common.agent_surface.compose_universal()` (Python)
or `memex agent-surface universal` (shell) if you are building a Memex-
aware agent beyond raw MCP."""


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
        RESOLUTION_FLOW → AXES → HISTORICAL_ROUTING → VIRTUAL_UNIT →
        KV_NAMESPACE → CITATIONS → CRITICAL_FOOTER

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
            RESOLUTION_FLOW,
            AXES,
            HISTORICAL_ROUTING,
            VIRTUAL_UNIT,
            KV_NAMESPACE,
            CITATIONS,
            CRITICAL_FOOTER,
        ]
    )


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
    'HISTORICAL_ROUTING',
    'KV_NAMESPACE',
    'RESOLUTION_FLOW',
    'RETRIEVAL_ROUTING',
    'STORAGE_MODEL',
    'VIRTUAL_UNIT',
    # Composer.
    'compose_universal',
]
