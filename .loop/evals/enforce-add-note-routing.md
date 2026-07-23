eval: enforce-add-note-routing

**Definition of Done:** `add_note` rejects a clearly how-to/procedure/worked-episode
body with a caller-correctable 4xx that names `case_submit`, while never rejecting a
legitimate note that merely contains steps, and honoring an explicit override.

Scoring policy: all rows are deterministic assertions on the ingest result / persisted
state at a hard 100% bar. The precision rows (2, 6) are the load-bearing guardrails —
a false rejection of a real note regresses the status quo, so they must pass 100%.

Fork-dependent rows are marked. They are written against the planner's recommended
resolution and must be re-pinned if the operator decides otherwise:
- Q6 (reject status code) → row 4 assumes **422**.
- Q4 (precision threshold) → row 6 assumes **≥2 co-occurring procedural signals**.
- Q3 (override name) → rows 3 assumes an explicit **`allow_procedural`** boolean.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** A how-to body is rejected, not persisted, and the error names `case_submit` | `add_note(markdown_content=<Trigger/Situation/Actions/Outcome worked-episode: "When X happens, first do A, then B, then C; outcome: fixed">)` | Call raises a caller-correctable error whose message names `case_submit`; **no** note row is persisted (note count unchanged) | Deterministic: assert error raised AND `note_count_after == note_count_before` AND `'case_submit' in error.message` | 100% |
| **[GUARDRAIL — precision]** A legitimate note that merely contains steps is accepted | `add_note(markdown_content=<a decision record: "We chose Postgres. Rationale… For reference, the migration runs as: 1. dump 2. restore 3. verify.">)` | Note persists normally; **no** rejection | Deterministic: assert note persisted AND no error raised | 100% |
| Explicit override bypasses the guard | Same procedural body as row 1, plus `allow_procedural=True` | Note persists; guard does not fire | Deterministic: assert note persisted AND no error raised | 100% |
| Rejection is caller-correctable (right status) *(Q6-dependent: 422)* | Procedural body as row 1, via the HTTP surface | HTTP **422** with a structured error body (not a 500), so the caller can correct and retry | Deterministic: assert `response.status_code == 422` | 100% |
| A plain fact note is unaffected (no behavior change today) | `add_note(markdown_content="The prod DB is Postgres 16, us-east-1.")` | Persists exactly as it does today; guard silent | Deterministic: assert note persisted AND no error raised | 100% |
| **[GUARDRAIL — threshold]** A single weak procedural signal does NOT trigger rejection *(Q4-dependent: ≥2 signals)* | `add_note(markdown_content=<a note containing one imperative sentence but no step structure: "Remember to rotate the creds quarterly.">)` | Note persists; one weak signal is below the rejection threshold | Deterministic: assert note persisted AND no error raised | 100% |
