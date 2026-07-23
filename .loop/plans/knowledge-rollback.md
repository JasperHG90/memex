# knowledge-rollback: append-only rollback of a note or mental model to a prior version

> **Epic:** part 5 of 5 of the knowledge-versioning epic (RFC #234).
> **Depends on:** `note-version-history` and
> `mental-model-version-history` (the version tables + read surface must
> exist). Independent of `knowledge-temporal-queries`.
> **Q4 decision (accepted default, PENDING USER CONFIRMATION):** note
> rollback routes through **re-ingest-and-re-extract** so derived state
> reconciles. The eval row for this behavior is flagged pending
> confirmation; if the user chooses body-only restore instead, re-pin
> that row and R2 before implementation.
> **Full delta investigation:** `.loop/knowledge-versioning-epic.md`.

## 1. Title

Add rollback for notes and mental models: append a NEW version whose
content equals a chosen prior version and apply it to the live row.
Rollback is append-only — history is never shortened or deleted. Because
Memex derived state survives deletion of its source (RFC #259), a note
rollback routes through the normal re-ingest path so extraction re-runs
and contradiction detection reconciles the derived units/models (Q4,
pending user confirmation).

## 2. Size / Effort

**M — a rollback service method per surface + one guarded MCP write tool
each.** The care is in the derived-state contract (Q4) and in keeping
rollback append-only (it must not mutate or delete any prior version).

## 3. Triggered by

RFC #234: "If a consolidation or mental model update introduces an error,
there is no way to revert." Rollback is the recovery path after a bad
ingest. Subticket 5 of the approved split.

## 4. Context (today's state, cited)

- **Version history exists** (`note_versions`, `mental_model_versions`,
  append-only, monotonic `version` per parent, unique
  `(parent_id, version)`) with `list_*`/`get_*_at` reads from subtickets
  2–3.
- **The note re-ingest path (Q4's mechanism).** Re-sending a note under
  the same `note_key` routes to the incremental-update branch
  (`ingestion.py:412-443`, `:432`) → `memory.retain(..., note_id=...)`
  (`:495-503`); this overwrites the body AND re-runs extraction, so
  routing rollback through it re-derives units and triggers contradiction
  detection (`contradiction/engine.py:340-376`). Doing so ALSO writes a
  new `note_versions` row via subticket 2's overwrite hook — that is the
  append-only rollback record.
- **Derived state is independent (RFC #259).** A note's memory units and
  the mental models built from them persist regardless of the note body.
  Restoring only the body would leave the note and its derivations
  disagreeing; re-ingest reconciles them at the cost of an LLM pass.
- **Mental-model write is CAS-guarded** (`reflection.py:722-729`);
  a mental-model rollback is a CAS `UPDATE` restoring a prior
  `observations` snapshot, which subticket 3's hook records as a new
  version.
- **Write-tool shape:** existing guarded MCP write tools (e.g. the
  outcome/status tools) with `ToolError` for 4xx; read template
  `server.py:409-474`.

## 5. Non-goals / out of scope

- No mutation or deletion of any prior version row — rollback is strictly
  append-only.
- No cross-object cascade rollback (rolling back a note does NOT roll back
  other notes, entities, or unrelated mental models to their state at the
  same timestamp — per the RFC's own "rollback is per-entity" answer).
- No entity-merge reversal (merges are forward-only hard deletes —
  `merge_entities.py:150`).
- No temporal `as_of`/diff (that is `knowledge-temporal-queries`).
- No `EvolutionTracker` analytics.

## 6. Requirements & restrictions

**Must achieve:**

- R1. `rollback_note(note_id, to_version)` sets the live note body to
  `to_version`'s content and records a NEW `note_versions` row
  (`change_type=rolled_back`, `change_reason` naming the target version).
  History length grows; no prior row is modified or removed.
- R2. **(Q4, pending user confirmation)** The note rollback routes through
  the re-ingest path so extraction re-runs and contradiction detection
  reconciles derived units/models — rather than a body-only restore. The
  tool contract states this explicitly (rollback re-derives). If the user
  confirms body-only instead, R2 and the eval row change to "body
  restored; derived state left as-is."
- R3. `rollback_mental_model(mental_model_id, to_version)` restores the
  prior `observations`/`entity_metadata` via a CAS `UPDATE`, recorded as
  a new `mental_model_versions` row (`change_type=rolled_back`). Append-
  only; the CAS `version` guard holds.
- R4. Rollback to a non-existent or future version returns a 4xx
  (`ToolError`), not a silent no-op.
- R5. Guarded MCP write tools `memex_note_rollback`,
  `memex_mental_model_rollback` (NOT `readOnlyHint`), mirroring the
  existing guarded-write shape; descriptions document the re-derive
  behavior (R2) and the 4xx trigger (R4).

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md`: tests-first (a test that rolls back
  and asserts history GREW); testcontainer Postgres; no
  `skip`/`xfail`/`# type: ignore`; gating tests in root `./tests/`.
- `CLAUDE.md:180-196`: MCP descriptions ≤ 1,200 chars, fenced by the MCP
  budget tests; SSOT description home. A write tool must not carry
  `readOnlyHint`.
- The mental-model rollback MUST respect the CAS `version` guard
  (`reflection.py:723`) — a concurrent bump must fail the rollback
  cleanly, not lose an update.
- Async I/O, single quotes, line 100, mypy strict.
- RFC #259 derived-copies insight is the basis for R2; keep the behavior
  an explicit, tested contract.
- `.claude/rules/pre-existing-issues.md`; `.claude/rules/adversarial-reviews.md`.

## 7. Code surface

- **Service** `packages/core/src/memex_core/services/notes.py` —
  `rollback_note` (routes through the re-ingest/`retain` path per R2). The
  mental-model versioning service — `rollback_mental_model` (CAS
  restore). **Edit.**
- **Facade** `packages/core/src/memex_core/api.py` — delegators. **Edit.**
- **Route** `packages/core/src/memex_core/server/notes.py` (and the
  mental-model router) — guarded `@router.post('/notes/{id}/rollback', ...)`
  (write auth dep, not `require_read`), `_handle_error` mapping the
  bad-version 4xx. **Edit.**
- **Client** `packages/common/src/memex_common/client.py` — `rollback_note`,
  `rollback_mental_model` (POST). **Edit.**
- **MCP** `packages/mcp/src/memex_mcp/server.py` — `memex_note_rollback`,
  `memex_mental_model_rollback` (guarded write, `ToolError` for 4xx).
  **Edit.**
- Read-only: `ingestion.py:412-443,495-503` (the re-ingest mechanism),
  `contradiction/engine.py:340-376`, `reflection.py:722-729`.

## 8. Tests & validation gates

**Gates:** `just test` + `just prek`. Run `uv run pytest packages/mcp/tests`
for the description fences.

**Reproducing test first — `tests/test_knowledge_rollback.py`** (root
`./tests/`, testcontainer Postgres):
- **[GUARDRAIL] Append-only:** seed a note with 3 versions; roll back to
  v1; assert the version count is now 4 (a new `rolled_back` row), all
  three prior rows are byte-for-byte unchanged, and the live body equals
  v1's content.
- **(Q4, pending confirmation) Re-derive:** after a note rollback, assert
  the derived memory units reflect the restored body (extraction re-ran)
  — i.e. rollback went through re-ingest. If the user picks body-only,
  this row asserts derived units are unchanged instead.
- Mental-model rollback: restores prior `observations`, records a new
  version, CAS guard holds under a concurrent version bump (rollback
  fails cleanly, no lost update).
- **Bad target:** rollback to a non-existent/future version returns 4xx,
  writes no version, leaves the live row unchanged.

**Eval marker (required):** `.loop/evals/knowledge-rollback.md` pins the
guardrail (rollback never shortens history) and the Q4-dependent
re-derive row (flagged pending user confirmation). Validate with
`loopctl eval knowledge-rollback`.

## 9. Risk assessment

- **Blast radius: writes to the live note/mental-model + re-runs
  extraction.** A rollback that mutates a prior version, or that skips
  the re-derive, breaks the core append-only/consistency contract.
  Mitigation: the append-only guardrail test; the re-ingest routing; the
  CAS-guard test.
- **Reversibility:** rollback is itself append-only, so a wrong rollback
  is corrected by another rollback — nothing is destroyed. Backing the
  feature out = remove the rollback methods/tools.
- **Failure modes:** (1) mutating/truncating history instead of appending
  — the guardrail test; (2) body-only restore leaving derived state
  stale when Q4 says re-derive (or the inverse if the user flips Q4) —
  the re-derive row, flagged; (3) CAS lost update on mental-model
  rollback — the concurrency test; (4) silent no-op on a bad target
  version — the 4xx test; (5) an LLM re-extraction on rollback
  re-contradicting newer facts — acceptable and expected under Q4
  (contradiction detection reconciles); document it in the tool contract.

## 10. Subtickets (ordered steps)

1. `rollback_note` via the re-ingest path (R1/R2). → verify: append-only
   guardrail + re-derive tests green.
2. `rollback_mental_model` (CAS restore, R3). → verify: mm rollback + CAS
   concurrency tests green.
3. Bad-target 4xx handling (R4). → verify: 4xx test green.
4. Routes + client + guarded MCP write tools. → verify:
   `uv run pytest packages/mcp/tests` green.
5. Adversarial review. → verify: clean; `just test` + `just prek` green.

## 11. Open questions

- **Q4 — PENDING USER CONFIRMATION.** Note rollback re-derive semantics.
  *Default (accepted by the team lead, awaiting the user): re-ingest-and-
  re-extract* so derived state reconciles. The alternative is body-only
  restore (fast, but note and derivations disagree) or restore-and-mark-
  stale. R2 and the re-derive eval row are written to the default; if the
  user confirms an alternative, re-pin both before this ticket is picked
  up. This is the one open decision in the epic.

---

**Eval marker:** `.loop/evals/knowledge-rollback.md`
(`require_eval: true`). The re-derive row is flagged pending user
confirmation of Q4.
