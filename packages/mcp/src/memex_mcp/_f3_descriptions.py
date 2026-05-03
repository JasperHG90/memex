"""Verbatim agent prompt text for F3 — 4-layer memory-routing primer.

Sourced from cognitive-memory-research-report.md §2.3 (Memory types and how
Memex covers them) + §4 F3 ("Agent prompt text"). When the spec changes the
verbatim parity test fails — that is the contract.

Three exported strings:
- ``LAYER_ROUTING_PRIMER_PROSE`` — compact prose for MCP tool descriptions,
  appended to the 5 search-tool descriptions where verbosity matters less
  than the tool listing.
- ``LAYER_ROUTING_PRIMER_TABLE`` — markdown table for the Hermes briefing
  + Claude Code rule + CLAUDE.md surfaces, where readability is preferred.
- ``LAYER_ROUTING_PRIMER_FRAGMENT`` — concise tool-call planning fragment
  for Hermes-side prompt templates (mirrors templates.py's
  ``RESOLUTION_FLOW_PROMPT_FRAGMENT`` pattern).

The four layers (verbatim from §2.3):
  - Episodic — Note (timestamped, source-attributed)
  - Semantic — MemoryUnit + MentalModel + Entity
  - Conceptual — synthesised MentalModel observations (reflection output)
  - Procedural-observations — KV with `procedure:` namespace (cross-agent
    observations about how to adapt skills, not the procedures themselves)
"""

from __future__ import annotations

LAYER_ROUTING_PRIMER_PROSE = (
    'Memex stores four memory layers. Pick the right tool for the layer you need:\n'
    '\n'
    '- Episodic ("what happened, when") → memex_note_search / memex_recent_notes /\n'
    '  memex_find_note. Notes are timestamped, source-attributed records of sessions,\n'
    '  reflections, and decisions.\n'
    '- Semantic ("decontextualised facts") → memex_memory_search /\n'
    '  memex_get_memory_units / memex_get_entity_mentions. MemoryUnits are short\n'
    '  fact/observation/event statements extracted from notes.\n'
    '- Conceptual ("synthesised mental models") → memex_survey /\n'
    '  memex_get_entities (with mental_models=True). MentalModels are reflection\n'
    '  output: strengthening / weakening / stable trends per entity.\n'
    '- Procedural-observations ("adaptations to context") → memex_kv_search /\n'
    "  memex_kv_get with prefix='procedure:'. Memex stores observations about how\n"
    '  to adapt your existing skills, not the procedures themselves.\n'
    '\n'
    'If unsure, default to memex_memory_search for content-shaped questions and\n'
    'memex_note_search for source-shaped questions ("show me the notes about X").'
)


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


__all__ = [
    'LAYER_ROUTING_PRIMER_FRAGMENT',
    'LAYER_ROUTING_PRIMER_PROSE',
    'LAYER_ROUTING_PRIMER_TABLE',
]
