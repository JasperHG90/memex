eval: ghost-memory-fact-succession

**Definition of Done:** a newly-ingested fact that supersedes an older one
without contradicting it is recorded as an append-only `supersedes` link +
a `superseded_by_unit_id` marker on the old unit; default retrieval returns
the current fact and not the superseded one; the superseded fact stays
reachable for historical/audit queries and in `get_unit_history`; and
succession never fires on additive facts or direct contradictions.

**Fork defaults encoded here (recommended, pending user confirmation):**
- **Q1 — a dedicated nullable `MemoryUnit.superseded_by_unit_id` column**
  (NOT reuse of `status='stale'`). Reusing `stale` would make superseded
  facts eligible for the `prune_stale_evidence` deletion job
  (`sql_models.py:2760-2763`), deleting historical facts and violating the
  retention invariant — the column avoids this and is the crisp, indexable
  filter predicate. Rows 1, 3, and 4 assert against this column; if the
  operator picks a different persistence, re-pin those rows.
- **Q2 — succession folds into the existing `ClassifyRelationships` LLM
  call** (a new `supersede` relation), so detection adds zero marginal LLM
  calls. Rows 6–7 guard that the folded prompt does not over-fire.
- **Q3 — no confidence decrement** on the superseded unit (succession is a
  temporal state, not evidence-of-wrongness). Row 1 asserts the London
  unit's confidence is unchanged.

Rows 1–5 are guardrails: deterministic, 100%. Rows 6–7 guard against
over-firing (succession must not swallow additive facts or direct
contradictions) and use `MockDspyLM` golden verdicts at ≥90%.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL — bank]** A non-contradictory succession is recorded as a link + marker, old fact retained | Ingest "user lives in London" (event_date 2024), then "user moved to Paris" (event_date 2025), same person entity | A `MemoryLink(link_type='supersedes', from=Paris unit, to=London unit)` exists; London unit's `superseded_by_unit_id` = Paris unit id; London unit's confidence is unchanged | Deterministic DB assert: link row exists with correct endpoints; column set; `london.confidence == pre_value` | 100% |
| **[GUARDRAIL — append-only]** The superseded unit is never edited or deleted | Same ingest as row 1 | London unit row still exists; its `text` is byte-identical to what was ingested; no delete occurred | Deterministic DB assert: row present AND `london.text == original_text` | 100% |
| **[retrieval]** Default retrieval returns the current fact, not the superseded one | `memory_search("where does the user live?")` with default flags (`include_superseded=False`, `apply_pre_filter=True`) | Paris unit is in the result IDs; London unit is NOT | Deterministic: `london_unit_id not in result_ids AND paris_unit_id in result_ids` | 100% |
| **[GUARDRAIL — audit reachability]** A historical/audit query still reaches the superseded fact | Same query run twice: once with `include_superseded=True`, once with `apply_pre_filter=False` | London unit appears in the result IDs under each bypass flag | Deterministic: `london_unit_id in result_ids` for both bypass runs | 100% |
| **[GUARDRAIL — history]** `get_unit_history` surfaces the succession chain | `get_unit_history(paris_unit_id)` after row 1's ingest | The returned predecessor tree contains the London unit, reached via a `supersedes` edge | Deterministic: London unit in `predecessors` with `link_type == 'supersedes'` | 100% |
| **[precision]** Additive, both-still-true facts are NOT marked superseded | Ingest "user likes coffee", then "user likes tea" (additive — both hold) | No `supersedes` link created between them; both units remain in default retrieval | `MockDspyLM` golden verdict: classify relation != 'supersede'; DB assert no supersedes link; both in default results | ≥90% of cases |
| **[precision]** A direct contradiction still routes to contradict/weaken, not silently to supersede | Ingest "the meeting is at 3pm", then "the meeting is at 4pm" (direct conflict on the same slot) | Relation is `contradict` or `weaken` (the confidence-signal path), NOT converted to `supersede`; no `supersedes` link created | `MockDspyLM` golden verdict: relation in {'contradict','weaken'}; DB assert no supersedes link | ≥90% of cases |
