# Note lifecycle review — post-FSFM

**Status:** review (decision record). Sprint ticket V7.
**Branch:** `release/tier-a-cognitive-memory`.
**Date:** 2026-05-11.

> **Note on `DESIGN_DOCUMENT.md` references in this document.**
> The canonical design document lives at the repo root as `DESIGN_DOCUMENT.md`. It is **`.gitignore`d** (`.gitignore:205`) and intentionally not under version control; it exists locally in the primary checkout. Section references in this review (e.g. "§2.4.4", "§3.3", "P6 in §1.2") point at that local artifact. Anchor verification therefore runs against the on-disk file, not the git tree.

## TL;DR — 30-second read

Lifecycle (`Note.status`) and FSFM (`MemoryUnit.is_deprioritized`) operate at different granularities, with different recovery paths and different retrieval-time filters. They do **not** collapse into each other.

Two of the four lifecycle states earn less of their keep post-FSFM: `archived` duplicates the supersede cascade and could become an FSFM-driven path; `appended` is a chronology marker, not a suppression state, and belongs on a FK relation rather than the status enum. Two behaviors need small patches: MW counters on supersession (undocumented retention, not a bug), and orphan inbound `contradicts` links to stale targets (hygiene gap, surface via lint).

| Question | Recommendation | Follow-up ticket |
|---|---|---|
| Q1 — `active` vs `is_deprioritized=true` | **Keep** | — |
| Q2 — MW counters on supersession | **Patch** | V13 — Implemented |
| Q3 — `archived` destructive cascade | **Consolidate** | V14 — Implemented |
| Q4 — `appended` as a status value | **Consolidate** | V15 — Implemented |
| Q5 — Hard overrides in FSFM | **Keep** | — |
| Q6 — Orphan `contradicts` links | **Patch** | V16 — Implemented |

---

## Q1. `active` vs `is_deprioritized=true` — are these overlapping?

**Recommendation: Keep.**

The two flags live at different granularities, carry different semantics, and have non-overlapping recovery paths.

- **Granularity.** `Note.status` (`packages/core/src/memex_core/memory/sql_models.py:269-278`) is a per-note string enum with CHECK constraint `status IN ('active','superseded','appended','archived')` at `packages/core/src/memex_core/memory/sql_models.py:312-315`. `MemoryUnit.is_deprioritized` (`packages/core/src/memex_core/memory/sql_models.py:596-600`) is a per-unit boolean. A note can be `active` while individual extracted units are `is_deprioritized=true`; the inverse holds too (note `superseded` → all units cascade to `status='stale'`).
- **Retrieval semantics.** `SearchService.search` exposes three independent filter flags — `include_stale`, `include_superseded`, `include_deprioritized` — at `packages/core/src/memex_core/services/search.py:65-67`. They are not aliased. A deprioritized active unit is filtered by `include_deprioritized=False` (default); a stale unit (from a superseded or archived note) is filtered by `include_stale=False`. Both default to hidden; the agent sees neither without an explicit opt-in.
- **Recovery.** A deprioritized unit recovers through the FSFM auto-band (composite score falls below the band threshold) or through the `memex_memory_restore` MCP tool; the `cooldown_days` knob gates the post-restore re-deprioritization window, not the band threshold itself. A stale unit recovers only through an explicit `Notes.set_note_status(...)` back to `active`, which cascades `unit.status = 'active'` for every unit on the note (`packages/core/src/memex_core/services/notes.py:248-258`).
- **FSFM short-circuit reinforces the distinction.** The stale short-circuit at `packages/core/src/memex_core/services/deprioritize_score.py:208-209` returns score 0 for stale units before running the composite — this is defense-in-depth (the retrieval-time filter is the primary gate), and explicitly relies on `status != 'stale'` being the precondition for FSFM to operate.

**Boundary to document in `DESIGN_DOCUMENT.md` §2.4.4:** lifecycle states are about *human intent* over notes; `is_deprioritized` is about *observed signal* over units. They cohabit one retrieval pipeline because both can suppress visibility, but they are not redundant.

---

## Q2. Superseded notes — do MW counters port to the superseder?

**Recommendation: Patch.**

Today, when a note is superseded, `Notes.set_note_status` enters the `superseded` branch at `packages/core/src/memex_core/services/notes.py:241-243` (writes `doc.superseded_by = linked_note_id`, then calls `_deactivate_note_units`); the cascade itself lives at `packages/core/src/memex_core/services/notes.py:279-299` and sets `unit.status = 'stale'` on every memory unit attached to the note (write at `:297`). The unit's `success_co_count` / `failure_co_count` columns are **not touched** and remain on the now-stale row. FSFM's `status == 'stale'` short-circuit at `packages/core/src/memex_core/services/deprioritize_score.py:208-209` means those counters are never read by the scorer — they are operationally inert.

There are two questions here:

1. **Should counters port to the superseder?** *No.* The superseder is, by Memex's append-only model, a new note with new units (`packages/core/src/memex_core/memory/sql_models.py:280-284`). Outcome attribution at the time the counters were incremented was against the old unit's text; porting would conflate contexts. The Beta-Bernoulli prior (`success+1, failure+1`) lets the superseder's new units start fresh at neutral.
2. **Are the inert counters a bug?** *Almost no, with one patch needed.* The audit trail is valuable — historical lineage is a stated invariant (P6 in §1.2 of `DESIGN_DOCUMENT.md`, gitignored). But re-activating a superseded note today (`Notes.set_note_status(note_id, 'active')`) cascades `unit.status = 'active'` at `packages/core/src/memex_core/services/notes.py:258` (inside the `elif status == 'active'` branch starting at `:248`) and *re-activates the original counters along with the unit*. That is correct (audit trail preserved) but currently undocumented; a reader expecting reset-on-reactivation will be surprised.

**V13 — Implemented.** Behavior is the right behavior, but it surprises readers expecting reset-on-reactivation, so the retention contract is now codified by tests and documented inline:

* **Counter retention on supersession.** `Notes.set_note_status(note_id, 'superseded')` cascades `MemoryUnit.status='stale'` but does NOT touch `success_co_count` / `failure_co_count`. Those counters are the unit's outcome-attribution history against its own text; preserving them keeps the audit trail (P6 invariant) intact.
* **FSFM short-circuits on stale.** `compute_composite` at `packages/core/src/memex_core/services/deprioritize_score.py:209-210` returns `(0.0, {}, True, 'status_stale')` before reading counters. The stale unit's counters are operationally inert while stale.
* **Reactivation restores counters.** `Notes.set_note_status(note_id, 'active')` flips `unit.status='stale' → 'active'` without touching the counters — they re-emerge with their pre-supersession values, exactly as the audit trail recorded them. From the perspective of the next outcome cycle (`record_outcome` → counter bump), the reactivated unit behaves as if it had been continuously active.
* **No port to superseder.** The superseder is, by Memex's append-only model, a new note with new units. Outcome attribution at the time the original counters were incremented was against the old unit's text; porting counters would conflate contexts. The Beta-Bernoulli prior (`success+1, failure+1`) lets the superseder's new units start fresh at neutral.

Test coverage:
* `packages/core/tests/unit/services/test_deprioritize_score.py::TestOverrides::test_status_stale_short_circuits_before_reading_counters` — parameterized across counter magnitudes (including a 1-million / 1 lopsided pair) asserts `compute_composite` returns `(0.0, {}, True, 'status_stale')` for stale units regardless of counter values; `components == {}` proves no scorer branch runs.
* `packages/core/tests/unit/test_note_service.py::TestSetNoteStatusCascade::test_mw_counters_preserved_across_supersede_reactivate` — full round-trip mock: seed `success=17, failure=4`, supersede (asserts status→stale + counters unchanged), reactivate (asserts status→active + counters still 17/4). The mock session captures the exact attribute writes the service performs; the absence of any write to the counter columns is the audit-trail invariant.

---

## Q3. `archived` — is the destructive cascade still differentiated from supersede?

**Recommendation: Consolidate.**

Operationally, `superseded` and `archived` are now indistinguishable in code: the `superseded` branch at `packages/core/src/memex_core/services/notes.py:241-243` and the `archived` branch at `packages/core/src/memex_core/services/notes.py:244-245` both call `_deactivate_note_units`, and the cascade body at `packages/core/src/memex_core/services/notes.py:279-299` is identical for both. The only branch difference is at `:242`: `superseded` writes `doc.superseded_by = linked_note_id`; `archived` skips the FK write. Wave 0 §3's framing of archive as a *destructive* cascade is misleading — neither transition actually destroys rows. Both leave the units as `status='stale'`, fully retained in `memory_units`, surfaced through the partial HNSW for stale units (`packages/core/src/memex_core/memory/sql_models.py:763-769`), and readable via `include_stale=True`.

The intent split is real: *superseded* says "this is replaced by note X"; *archived* says "I'm done with this." But the operational cost of carrying two states for one cascade is non-zero — `NoteNotAppendableError` (raised at `packages/core/src/memex_core/services/ingestion.py:743-747`; docstring at `:561` enumerates archived/superseded as the two non-appendable parent states) gates both equally, retrieval flags treat both via `include_superseded` / `include_stale`, and the agent surface (`memex_set_note_status`) exposes both verbs with effectively identical downstream behavior.

Post-FSFM, the better factoring is:
- `archived` = `is_deprioritized=true` cascade across the note's units + a `note.archived_at` timestamp on `Note` (intent retained).
- Loses the dedicated status value; gains FSFM-uniform suppression semantics (score-aware, auto-band-recoverable instead of requiring an explicit `set_note_status('active')`).

**V14 — Implemented.** Migrated `archived` semantics into FSFM in alembic revision `041_archived_fsfm`. **Known limitation**: a unit explicitly deprioritized via `memex_memory_deprioritize` BEFORE its parent note is archived will lose its per-unit deprioritize signal when the note is later reactivated — the reactivation path resets `is_deprioritized=False` on every unit of an archived note (the `was_archived` gate prevents the same clobbering when the note was NOT archived, e.g. supersede→active). Closing this fully would require a per-unit cascade-provenance signal (e.g. a separate `is_deprioritized_reason` column or a deprioritized-by-archive bitmap); deferred until operator demand surfaces. Operators who need the distinction today can re-issue the per-unit deprioritize after reactivation. `archived` is dropped from the `ck_notes_status` CHECK enum; `Note.archived_at` (nullable `TIMESTAMP(timezone=True)`, indexed) is added as the human-intent signal. The agent-facing call `Notes.set_note_status(note_id, 'archived')` now records `Note.archived_at = now()` and flips every unit of the note to `is_deprioritized=true` via the new `_deprioritize_note_units` helper (parallel to `_deactivate_note_units`, which retains the supersession-stale cascade unchanged). Storage-side the note row stays at `status='active'`. `list_notes(status='archived')` translates to `Note.archived_at IS NOT NULL`; `list_notes(status='active')` excludes archived (`status='active' AND archived_at IS NULL`). Reactivation via `set_note_status(note_id, 'active')` clears `archived_at` and resets every unit's `is_deprioritized=false` alongside the existing `status='stale' → 'active'` reactivation. Evidence pruning + entity-reflection queueing now fire only on `superseded`, not on `archived` (those mechanisms are the supersession contract, not archive's). The migration backfills `archived_at = updated_at`, `is_deprioritized=true`, and `status='active'` for any rows still at `status='archived'`. Agent-surface parity: MCP and Hermes `memex_set_note_status` tool descriptions now spell out the dual cascade (supersede → stale, archive → FSFM) and name `memex_memory_restore` + `include_deprioritized=True` as the restoration / inspection path.

---

## Q4. `appended` — status value or relation-only concern?

**Recommendation: Consolidate.**

`appended` is the only one of the four states that is not a *lifecycle* status in the usual sense. The `Notes.set_note_status` path at `packages/core/src/memex_core/services/notes.py:246-247` writes `doc.appended_to = linked_note_id` but does **not** call `_deactivate_note_units` — the note keeps its `MemoryUnit` rows in `status='active'`, fully indexed, fully retrievable. The only structural effect is the `Note.appended_to` FK at `packages/core/src/memex_core/memory/sql_models.py:286-290`.

In the post-FSFM landscape, the status enum's four values divide cleanly:
- `active` — default visibility.
- `superseded` — stale-cascade + replacement FK.
- `archived` — stale-cascade (today) or FSFM suppression (per Q3).
- `appended` — *not* a suppression state; a chronology marker.

`appended` belongs alongside `superseded_by` as a structural FK relation, not on the status enum. The benefit is symmetry: the enum becomes a suppression-state enum (`active` / `superseded` / `archived`), the FK columns (`superseded_by`, `appended_to`) carry the relational chronology. Today's `Notes.set_note_status('appended', linked_note_id=X)` becomes `Ingestion.append_to_note(parent_note_id=X, ...)` semantically (the actual ingestion path at `packages/core/src/memex_core/services/ingestion.py:534` already does this through `NoteAppend`; the `status='appended'` value is the legacy duplicate).

**V15 — Implemented.** Dropped `'appended'` from the `Note.status` CHECK enum in alembic revision `041_drop_note_status_appended`. The migration backfills `status='active'` for any rows still at `status='appended'` (their unit-level state was already active) and recreates `ck_notes_status` with `('active', 'superseded', 'archived')`; downgrade re-adds `'appended'` to the constraint without data restoration. The actual append chronology is owned by the `NoteAppend` audit table: `Ingestion.append_to_note` extends content in-place on the parent's `note_id` and writes one `NoteAppend` row per call. The `Note.appended_to` column survives the schema change as a residual structural FK, but post-V15 the only writer is the reset path on transition-to-active (`services/notes.py:264` — `doc.appended_to = None`); no path sets a non-NULL value any more. The only pre-V15 non-NULL writer was `Notes.set_note_status('appended', linked_note_id=X)`, which V15 removes. Read paths still surface the column (lineage response shape, snapshot export). A future ticket can either populate `appended_to` from `Ingestion.append_to_note` for read-side parity with `Note.superseded_by`, or drop the column outright after the read paths are reworked. `Notes.set_note_status('appended', ...)` raises a `ValueError` pointing the caller to `memex_append_note` (or `POST /notes/append`) before touching the database. Agent-surface parity: the MCP and Hermes tool descriptions for `memex_set_note_status` enumerate `active / superseded / archived`, the Hermes `_VALID_NOTE_STATUSES` frozenset rejects `'appended'` client-side, and the schemas' `enum` lists are reduced to the three remaining values; the recommended append path (`memex_append_note`) is named in both tool descriptions.

---

## Q5. Hard overrides in FSFM — is the lifecycle layer redundant?

**Recommendation: Keep.**

FSFM hard overrides at `packages/core/src/memex_core/services/deprioritize_score.py:205-213` short-circuit the composite scorer for four conditions:

1. `is_deprioritized=true` (check at `:206`) → score 0 (already deprioritized; idempotency).
2. `status == 'stale'` (check at `:208`) → score 0 (lifecycle layer already filtered).
3. `risk_class in {'sensitive','private','safety'}` (`PROTECTED_RISK_CLASSES` declared at `packages/core/src/memex_core/services/deprioritize_score.py:66`; check at `:210`) → score 0 (PII / safety floors).
4. `intent_class == 'permanent'` (check at `:212`) → score 0 (write-time importance assertion).

Each override addresses a non-overlapping concern. The lifecycle short-circuit (#2) is **not** redundant with the retrieval-time `include_stale=False` filter at `packages/core/src/memex_core/services/search.py:65-67`. They are layered:

- The retrieval filter prevents stale units from being *served*.
- The FSFM short-circuit prevents stale units from being *scored* (and thus from being mis-deprioritized further, or from polluting the score histogram).

Without the FSFM short-circuit, a future code path that surfaces stale units in some bespoke flow (export, audit, debugging) could leak deprioritization scoring against rows whose `success_co_count` / `failure_co_count` are intentionally inert (per Q2). Defense-in-depth.

The risk and intent overrides are orthogonal to lifecycle. A `risk_class='sensitive'` unit must be protected from auto-deprioritization regardless of its note's status; a `intent_class='permanent'` unit must never decay regardless of how its note has aged.

**Boundary to document in `DESIGN_DOCUMENT.md` §2.4.4:** lifecycle state and risk/intent class are independent axes that both inform FSFM's hard overrides. Removing the lifecycle layer (Q3) does not change this layering; the override on `status=='stale'` continues to apply (or, post-Q3-merge, the override becomes `is_deprioritized=true` — same effect, narrower enum).

---

## Q6. State transitions and orphan `contradicts` links

**Recommendation: Patch.**

`MemoryLink` carries ten typed relations including `contradicts` (CHECK at `packages/core/src/memex_core/memory/sql_models.py:1344`). When a note is superseded or archived, `_deactivate_note_units` (`packages/core/src/memex_core/services/notes.py:279-299`, with the `unit.status = 'stale'` write at `:297`) sets every memory unit attached to the note to stale but leaves all inbound and outbound `MemoryLink` rows untouched. Specifically:

- If unit U on note N is contradicted by unit V on note M (`MemoryLink(from_unit_id=V, to_unit_id=U, link_type='contradicts')`), and note N is archived, the link row survives. Unit U is now stale; unit V still surfaces.
- At retrieval time, V's confidence may already have been lowered at extraction time by the contradiction-detection pass against U (the contradiction event runs at ingestion, not at scoring; the scoring path reads `unit.confidence` via the `confidence_alpha` reranker term — so V keeps carrying a lowered-confidence signal even though U is no longer reachable).

This is **not** a correctness bug — FSFM's `status=='stale'` short-circuit (Q5) and the retrieval-time `include_stale` filter (Q1) ensure stale units don't surface in default flows. But it is a hygiene issue: the `MemoryLink` table accumulates rows pointing at stale targets indefinitely; the lint dashboard (`packages/core/src/memex_core/services/lint.py`) has no rule to surface this.

Two options:

- **Reap on transition.** On supersede/archive, delete inbound `contradicts` links where the target is now stale. Loses audit history (the contradiction *did* exist and may have influenced past retrievals).
- **Surface via lint.** Add a lint rule `orphan_contradicts_links_post_stale` that proposes (does not auto-apply) reaping when accumulation exceeds a threshold. Keeps audit history; gives the operator the choice.

The second option aligns with the non-destructive-curation invariant (P1).

**V16 — Implemented.** Lint rule `orphan_contradicts_links_post_stale` is registered in `V1_RULES` at `packages/core/src/memex_core/services/lint.py` with `lint_type=LintType.QUALITY` and `target_type='memory_unit'`. The SQL predicate aggregates per active source unit: `MemoryLink.link_type='contradicts' AND target_unit.status='stale' AND source_unit.status='active' AND link.vault_id=:vault_id`. The partial unique index `(rule_name, target_type, target_id, vault_id) WHERE status='pending'` collapses multiple stale targets reached from the same source into a single proposal; `evidence` carries `orphan_link_count`, the list of `stale_target_unit_ids`, and the oldest `stale_targets_oldest_updated_at` so the operator can navigate to the source unit and reap the orphan edges through the lint dashboard. Surfaces via `memex_get_lint_flags` (HTTP, MCP, CLI) without any surface code change — the `lint_type` query path already accepts the new rule. Observability: the existing `memex_lint_findings_total{rule_name, lint_type, vault_id}` Counter (`packages/core/src/memex_core/metrics.py`) satisfies the per-vault accumulation signal at `rule_name='orphan_contradicts_links_post_stale'`; no dedicated `memex_orphan_contradicts_links_total{vault_id}` counter is shipped because it would duplicate the same series under a narrower name. Tests: `packages/core/tests/integration/services/test_int_lint.py` extends `_seed_all_rules_fire` to seed the orphan case (asserting `total_findings == 5` and idempotent re-run), and `packages/core/tests/integration/services/test_int_lint_orphan_contradicts.py` carries focused negative-case tests (active target, cross-vault scoping, multi-target aggregation, non-`contradicts` link-type rejection). Migration: none (read-only lint rule).

---

## Summary table — proposed follow-ups

| ID | Title | Scope | Migration | Agent-surface | Eval |
|---|---|---|---|---|---|
| V13 | Document supersession→stale→counter retention | Doc + unit test | N/A | N/A | unit test only |
| V14 | Migrate `archived` into FSFM (`is_deprioritized` cascade) | Schema + service | Drop `archived` from `Note.status` enum; add `Note.archived_at`; backfill units | `memex_set_note_status('archived')` dispatch change | internal-regression scenario |
| V15 | Drop `appended` from `Note.status` enum | Schema + service | Backfill `status='active'` for existing `appended` rows | `memex_set_note_status` rejects `'appended'` | regression scenario |
| V16 ✅ | Lint rule for orphan `contradicts` links to stale targets | Service + lint | N/A | new `MaintenanceProposal` shape via `memex_get_lint_flags` | integration scenario |

Each follow-up file declares its own full DoD, anchors, parity, and eval coverage when it's authored via `memex-feature-plan`.

---

## What this review does *not* do

- No code change, no migration, no agent-surface change, no test change, no eval change land in this PR. The review is a decision document.
- `DESIGN_DOCUMENT.md` §2.4.4 (Curate) and §3.3 (Note row) are not edited here — the boundary documentation called out under Q1 and Q5 lands alongside whichever follow-up implements behavior change (or as its own small doc-only PR).
- The four follow-up IDs (V13–V16) are *proposed*; the BACKLOG entries are appended in this PR but the implementations are separate tickets.
