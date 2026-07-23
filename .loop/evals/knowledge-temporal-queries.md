eval: knowledge-temporal-queries

**Definition of Done:** `as_of` reconstruction returns the note (and mental-model)
content of the version that was live at a given timestamp; a between-versions diff
reports the change; and the new note/mental-model `as_of` axis stays strictly
separate from the pre-existing entity-cooccurrence `as_of` (they never cross).

Scoring policy: all rows are deterministic assertions against testcontainer Postgres,
at a hard 100% bar. The axis-independence row is the load-bearing guardrail — the one
real risk is conflating the two `as_of` axes.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** `as_of` reconstructs the version live at T | A note with three versions at `t1 < t2 < t3`; `get_note_at(as_of=T)` for `t2 < T < t3` | Returns v2's content (the latest version with `created_at <= T`) | Deterministic: assert returned content equals v2 | 100% |
| `as_of` boundaries resolve correctly | `get_note_at(as_of=T)` for `T` after `t3`, and for `T` before `t1` | After `t3` returns the current body; before `t1` returns the declared sentinel (empty/not-found) | Deterministic: assert both boundary results | 100% |
| A between-versions diff reports the change | `diff_note_versions(K, from_version=1, to_version=3)` | The diff names the lines that changed between v1 and v3 | Deterministic: assert the changed lines appear in the diff | 100% |
| Mental-model `as_of` reconstructs historical observations | `get_mental_model_at(id, as_of=T)` for a T between two refreshes | Returns the observations live at T | Deterministic: assert historical observations returned | 100% |
| **[GUARDRAIL]** The two `as_of` axes stay independent | A note `as_of` query in a vault that also has `EntityCooccurrence` rows with `valid_from`/`valid_to`; and a cooccurrence `as_of` retrieval in a vault with note versions | The note `as_of` result is unaffected by any cooccurrence validity window, and the cooccurrence `as_of` retrieval is unaffected by note versions | Deterministic: assert each query's result is independent of the other axis's temporal data | 100% |
