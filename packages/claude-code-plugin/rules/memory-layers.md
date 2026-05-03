## Memory layers and tool routing

Memex stores four memory layers. Pick the right tool for the layer you need.

| Layer | What it stores | Retrieve with | Tiny example |
|---|---|---|---|
| **Episodic** ("what happened, when") | Timestamped, source-attributed Notes — sessions, reflections, decisions | `memex_note_search` / `memex_recent_notes` / `memex_find_note` | "Find yesterday's reflection about the deploy regression" |
| **Semantic** ("decontextualised facts") | MemoryUnits — short fact/observation/event statements extracted from notes | `memex_memory_search` / `memex_get_memory_units` / `memex_get_entity_mentions` | "What does v2 use for auth?" |
| **Conceptual** ("synthesised mental models") | MentalModels — reflection output bundling per-entity observations with trend tracking (new/strengthening/stable/weakening/stale) | `memex_survey` / `memex_get_entities` (with `mental_models=True`) | "What do you know about Project X overall?" |
| **Procedural-observations** ("adaptations to context") | KV entries under `procedure:<verb>:<context-tag>` — observations about how to adapt your existing skills to a context, NOT the procedures themselves | `memex_kv_search` / `memex_kv_get` with `prefix='procedure:'` | "For this user, `deploy` means staging — never prod after 6pm" |

### Rule of thumb

If unsure, default to `memex_memory_search` for content-shaped questions
("what about X?") and `memex_note_search` for source-shaped questions
("show me the notes about X"). Run both in parallel when the user genuinely
needs distilled facts AND source notes.

The agent owns the verb (the executable how-to — your skills); Memex owns
the adverb (observations about how to adapt the verb to a specific user,
project, or codebase). Procedural memory in Memex is *observations about
procedures*, never the procedures themselves — see
`cognitive-memory-research-report.md` §2.3.1.

### Out of scope (do NOT request these)

Core / Cross-Context layers (per ZenBrain's 7-layer expansion) are
informational only and not first-class in Memex today. Pinned MentalModels
and global Entities cover them implicitly; do not request new schema or
new tools for these layers.

Canonical sources: `cognitive-memory-research-report.md` §2.3 (memory-types
mapping), §2.4 (current-services reality table), §4 F3 (this rule's spec).
The code-level single source of truth for the primer strings lives in
`packages/common/src/memex_common/agent_surface.py`; this rule file
mirrors the four canonical layer rows from that module's
`LAYER_ROUTING_PRIMER_TABLE`, with row-level verbatim parity enforced by
`packages/hermes-plugin/tests/test_f3_layer_primer_parity.py::test_table_surfaces_carry_full_canonical_rows`.
