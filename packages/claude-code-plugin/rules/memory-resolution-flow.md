## Memory resolution flow — the 5-step pattern

When the user reports an issue resolved ("the X bug is fixed", "we shipped Y",
"issue Z is no longer relevant"), follow §3.5 of the cognitive-memory research
report exactly. Skipping any step (especially Step 1 disambiguation or Step 3
LLM judgment) leads to bulk-writing irrelevant units.

### Step 1 — Disambiguate first

If the scope is ambiguous, ASK before writing. Examples that warrant a
clarification turn:

- "telegram issues from yesterday" when there are three reflection notes
  mentioning Telegram in the last week
- "the auth bug we discussed" with no temporal anchor
- multiple distinct issues conflated into one statement ("the issues are
  fixed" — which ones?)

### Step 2 — Route by info quality, then pick a coverage path

| Information user supplied | Right tool |
|---|---|
| Title fragment ("yesterday's reflection") | `memex_find_note(query="...")` — title-fragment lookup, indexed, cheap |
| Content only ("the standup notes about the deploy regression") | `memex_memory_search(query="...")` — full pipeline, expensive but right when title is unknown |

Then pick ONE of three coverage paths:

- **Option A — entity-anchored (highest recall when topic ↔ entity)**:
  `memex_list_entities(query="telegram")` → `memex_get_entity_mentions(entity_id=...)`.
  Structural traversal across every note; no semantic-rank miss.
- **Option B — cross-note semantic (when no entity anchor)**:
  `memex_memory_search(query="...", after="...", top_k=30)`. **CRITICAL: `top_k`
  must be ≥30** — the default 5 is too narrow and will miss cross-note matches.
- **Option C — single-note PageIndex traversal (provably one note)**:
  `memex_get_page_indices(note_id)` → read chunk summaries → pick fix-relevant
  chunks → `memex_get_memory_units(chunk_ids=[...])`. Captures every unit in
  the chunk; semantic top-k can miss paraphrased mentions.

### Step 3 — Mandatory LLM judgment (NEVER bulk-write)

Whichever path returned candidates, READ the unit bodies and judge which ones
actually correspond to the user's claim. Memory units are short by design
(single fact / observation / event, ~1–3 sentences) — the search response IS
the content; reading does not blow up context. The judgment cannot be skipped:
a daily-reflection note contains episodic observations ("worked on memex 3h
today") that look superficially relevant but are not fix-targets.

### Steps 4 + 5 — Paired writes against the LLM-judged-relevant subset

For each unit the LLM judged relevant, issue BOTH writes:

```
memex_record_outcome(unit_ids=[...], success=false, reason="user confirmed fixed")
memex_memory_deprioritize(unit_id=..., reason="user confirmed fixed YYYY-MM-DD")
```

Both, against the SAME subset. The two verbs are orthogonal axes:

| Tool | Question it answers | Cardinality | Reversible? |
|------|---------------------|-------------|-------------|
| `memex_record_outcome(success=...)` | "Did this memory help when retrieved?" | Append-only counter (compounds across retrievals) | No (audit log) |
| `memex_memory_deprioritize(reason)` | "Should this surface by default at all?" | Binary state on the unit | Yes (`memex_memory_restore`) |

You can want one without the other:

- **outcome=false but no deprioritize**: gradient signal only — let MW
  compound; perhaps a different query still legitimately wants this unit.
- **deprioritize but no outcome**: verdict without judging past usefulness
  ("correct when written but no longer relevant").
- **BOTH** (the user-confirmed-fix case): negative-usefulness AND binary
  verdict.

### Imperfect recall is by design

None of Options A/B/C give *provable* 100% recall on cross-note resolution.
Semantic search misses paraphrases; entity traversal misses oblique references;
chunk-scoped reads miss issues split across chunks. **F33 exploration is the
safety net** — any unit that slipped past resolution will occasionally
re-surface, the user re-confirms, and another `record_outcome(success=false)`
compounds the MW penalty. User-driven resolution is a GRADIENT across many
turns, not a one-shot delete.

## Historical / audit-query routing rule

The 5-step flow assumes the user is asking "what's true *now*". For queries
about HOW THINGS CHANGED — "how has my view on X evolved", "what did I used to
think about Y", "show me everything I believed about Z including the wrong
stuff" — route differently.

**Disambiguation triggers** (any of these → use the historical rule, NOT the
resolution flow): "evolved", "used to", "history of", "what changed", "what
did I think before", "audit", "show me everything", "show me the hidden ones",
explicit time-window-with-no-filter intent.

- **Ordered-chain timeline on a specific unit**:
  `memex_get_unit_history(unit_id)` (F49) — graph walk through contradiction
  links, returns predecessors in temporal order. Cleaner semantics than ranked
  search for "evolution" queries.
- **Broader audit / "show me everything including hidden stuff"**:
  `memex_memory_search(query="...", apply_pre_filter=False)` — bypasses F40 +
  F48 (MW + FSFM + confidence pre-filters) so contradicted,
  behaviorally-failed, and decayed units appear. Post-reranker boosts (F47
  confidence_boost, F1c MW) still apply, so contradicted units rank below
  clean ones — which is correct ordering for audit queries.

When the user says "the X issue is resolved" → resolution flow (Steps 1–5).
When the user says "how has my position on X changed" → historical routing.
Disambiguation is the agent's responsibility.

## What is NOT a gap (do NOT request these)

The resolution flow uses only existing primitives. Do NOT request any of the
following — the existing tools + the orthogonal-axes composition are
sufficient:

- A combined `memex_resolve(unit_ids, reason)` endpoint. Hides the orthogonal
  axes (gradient outcome vs binary surface state). Compose at the agent layer.
- A `resolved_at` timestamp column. The maintenance ledger already records
  when deprioritize fired; the note's `created_at` anchors the original
  observation.
- A `resolution_type` enum on deprioritize. Free text in `reason` carries the
  same information without committing the schema to a closed taxonomy.
- A `bulk-by-source` parameter on deprioritize. Agents iterate.
- Note-level deprioritize. Notes are episodic anchors; deprioritization
  applies to the derived units, not the source notes.
