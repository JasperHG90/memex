eval: invariant-distillation-from-corrections

**Definition of Done:** the distillation pass mines a recurring correction
cluster, files a human-gated proposal (never auto-materializing and never
fabricating a memory unit), and on explicit approval materializes exactly one
invariant carrying lineage back to the correction events that produced it.

Scoring policy: every row is a deterministic DB/state assertion at a hard
100% bar — the invariants here are structural (a row exists / no unit is
written / ids match), so no model judgment is needed. Rows 1, 3, and 6 are
guardrails and MUST pass 100%.

Note on row 2: the "materialized artifact" assertion is **Q2-dependent**. It
is written against the ticket's recommended materialization target — a new
first-class `DerivedInvariant` object with `source=synthesis` and a
`derived_from` field. If the operator resolves §11 Q2 to "KV entry with
lineage", re-pin this row's Expected/Scorer to assert the KV entry and its
embedded/columnar lineage instead. The guardrail rows (1, 3, 6) hold
regardless of how Q2 resolves.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** A recurring correction is proposed, never auto-materialized | Seed one vault with the same `not_helpful` correction (reason "stop suggesting X") recorded on 3 distinct `unit_id`s via `record_outcome`, then run the distillation pass once | Exactly one `MaintenanceProposal` row with `status='pending'` and the `materialize_invariant` action pre-selected; **zero** KV entries written and **zero** derived-invariant rows created | Deterministic: query the proposal ledger + KV table + derived-invariant table and assert `pending_count==1 AND kv_written==0 AND invariant_rows==0` | 100% |
| Approval materializes exactly one invariant WITH lineage *(Q2-dependent — see note)* | With the pending proposal from row 1, approve it via the lint apply path (`lint_apply`) | Exactly one materialized `DerivedInvariant` (`source='synthesis'`) exists; its `derived_from` equals the set of the 3 source correction event ids (the `OutcomeAuditLog`/unit ids that produced the candidate) | Deterministic: assert `invariant_rows==1` AND `set(row.derived_from) == {the 3 seeded correction ids}` | 100% |
| **[GUARDRAIL]** Never fabricates a MemoryUnit | Run the full propose → approve cycle from rows 1–2 to completion | No `MemoryUnit` with `source='synthesis'` — and no net-new `MemoryUnit` of any kind — is created at any step of propose or approve | Deterministic: snapshot `MemoryUnit` count + assert no unit with `source='synthesis'` exists before/after | 100% |
| Rejection is logged and materializes nothing | With the pending proposal from row 1, reject it via the lint resolve/dismiss path | Proposal `status='dismissed'`; **zero** invariants materialized (no KV entry, no derived-invariant row) | Deterministic: assert `proposal.status=='dismissed' AND kv_written==0 AND invariant_rows==0` | 100% |
| A below-threshold cluster produces no proposal (anti-nag) | Seed a correction cluster that recurs only 2 times (recurrence threshold = 3), then run the distillation pass | Zero `MaintenanceProposal` rows filed for that cluster | Deterministic: assert `pending_count==0` after the pass | 100% |
| **[GUARDRAIL]** Vault scoping — no cross-tenant leak | Seed threshold-crossing corrections in vault A and unrelated corrections in vault B; run distillation scoped to vault A | The filed proposal cites only vault-A correction ids; no vault-B id appears in its evidence/lineage, and no proposal is filed under vault B | Deterministic: assert every cited correction id ∈ vault A AND no proposal exists for vault B | 100% |
| Go/no-go spike counts and decides correctly | Two fixtures: (a) a stream with 5 distinct corrections each recurring 3×; (b) a sparse stream below threshold | Fixture (a) → spike emits **GO**; fixture (b) → spike emits **NO-GO**, matching the stated recurrence threshold | Deterministic: assert `spike(a)=='GO' AND spike(b)=='NO-GO'` | 100% |
