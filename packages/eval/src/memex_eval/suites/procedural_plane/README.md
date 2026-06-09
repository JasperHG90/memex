# procedural_plane

V7 procedural-plane eval suite. 10 scenarios pin the public contract of
the third Memex memory plane (case / procedure / strategy) and gate
write-routing for the agentic surfaces (Hermes briefing block, Claude
Code SessionStart hook).

## Scope

**In scope** — V7 procedural-plane behaviour that an agent depends on:

- Identity-anchor uniqueness and idempotency
  (`(kind, scope, verb, context)` UNIQUE NULLS NOT DISTINCT)
- 3 kinds — `case`, `procedure`, `strategy` — and their distinct
  shape rules (case has `trigger`, procedure/strategy have
  `verb`+`context`)
- 4 scopes — `global`, `user`, `project:<id>`, `app:<id>`
- Write surface: 8 HTTP routes
  (create / upsert / get / get_by_identity / update / deprecate /
  search / briefing_cards)
- Hybrid search composition (BM25 + vector + RRF)
- Briefing-cards pin chain
  (`global → project → app`, union, sorted by pin position)
- Lifecycle: `deprecate` removes from default
  `status='published'` search, entry remains reachable via `get`
- Status filter override (`status='all'` includes drafts)

**Out of scope** — things the suite deliberately does NOT cover:

- Extraction-driven upsert (V7 is a direct write surface; the suite
  bypasses LLM extraction via `procedural_upsert` setup action so the
  seeded state is deterministic)
- Cross-vault routing (V7 is vault-scoped like the note plane)
- Lineage tracking (V7 entries don't carry upstream/downstream
  provenance chains)
- Backfill / migration from V6 to V7 (handled by the V6→V7 migration
  suite, not here)
- Performance / latency (covered by the benchmark suite under
  `packages/eval/benchmarks/`)

## Scenarios

| # | ID | What it pins |
|---|----|--------------|
| 1 | `identity_anchor_collision_returns_409` | Two creates on the same anchor → 409 |
| 2 | `upsert_on_existing_anchor_updates_in_place` | Upsert is idempotent on the anchor |
| 3 | `get_by_identity_returns_seeded_entry` | Cheap "have we learned this?" probe |
| 4 | `get_by_identity_returns_404_when_unbound` | 404 distinguishes "new" from "existing" |
| 5 | `search_returns_seeded_procedure` | Hybrid BM25+vector+RRF composition works |
| 6 | `briefing_cards_pin_chain_union` | Pin chain returns the union, not just most-specific |
| 7 | `briefing_cards_pin_position_order` | Cards sorted by pin (global first) |
| 8 | `deprecate_drops_from_published_search` | Deprecate fades from search, not from `get` |
| 9 | `status_all_includes_drafts` | Drafts surface under `status='all'` |
| 10 | `case_kind_roundtrip` | Case shape (trigger, no verb) round-trips |

## Running

```bash
# From /home/vscode/worktrees/v7-procedural-experiential
memex-eval suite run procedural_plane
memex-eval suite list  # confirms the suite is discoverable
```

The suite requires a running Memex server with the V7 procedural
plane enabled (`server.memory.procedural.enabled=true`) and an
LLM-free setup path — the seeded entries are written directly via
the `procedural_upsert` API call, bypassing the LLM extraction
pipeline.

## Design notes

The suite is **read-side heavy** — 8 of 10 scenarios are
`procedural_search_results` or `procedural_entry_roundtrip`, both of
which read back the seeded state. The other 2 are pure
write/lifecycle scenarios (collision, deprecate). The split mirrors
the V7 contract: the plane is a write surface, but the agent's value
is in the read path (search, briefing cards, identity probe).

**Determinism** — setup actions use deterministic UUIDv5 anchored on
the `(_PROC_NS, kind, scope, verb, context, title)` tuple, so
re-running the suite produces the same state across machines. The
fixture namespace is `f1a2b3c4-d5e6-4a7b-8c9d-0e1f2a3b4c5d` (purely
a repeatability anchor; not a production namespace).

**Idempotency** — `procedural_upsert` is idempotent on the identity
anchor, so re-running the suite on a dirty vault produces the same
state. Teardown deprecates rather than deletes (audit trail
preservation).

**Context references** — scenario 8 chains two setup actions via the
`$procedural_upsert.entry_id` context reference, which the runner
substitutes with the first action's return value. This is the
framework primitive for multi-step setup pipelines.
