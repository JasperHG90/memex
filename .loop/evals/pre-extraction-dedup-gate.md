eval: pre-extraction-dedup-gate

Definition of Done: when admission is enabled, content that exactly or near-duplicates existing memory is SKIPPED before any extraction LLM call; everything else PROCEEDS; when admission is disabled the ingest path is unchanged.

Scenarios exercise the pure verdict seam `decide(hash_hit, top_similarity, threshold)` plus the enabled/disabled and vault-scoping invariants. All rows are deterministic (no DB, no model) so they run offline in the `just test` loop gate.

Defaults encoded from ticket §11 recommendations; open-question dependencies noted in the Behavior cell so a settled answer that differs forces a marker update.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| Exact content-hash duplicate is skipped | `hash_hit=True` (a note whose content_hash equals `note.content_fingerprint` already exists in the target vault under a different note_key), `top_similarity=None` | verdict is `SKIP`; ingest returns `{'status':'skipped','reason':'admission_redundant'}` and evidence carries `hash_hit=true` | deterministic check | 100% |
| Near-duplicate above threshold is skipped | `hash_hit=False`, `top_similarity=0.95`, `threshold=0.92` (normalized cosine of raw-content embedding vs nearest active chunk) | verdict is `SKIP`; evidence carries `top_similarity=0.95` and the matched note id | deterministic check | 100% |
| Content just below threshold proceeds (precision guard) | `hash_hit=False`, `top_similarity=0.91`, `threshold=0.92` | verdict is `PROCEED`; nothing is skipped; extraction runs | deterministic check | 100% |
| Novel content with no candidates proceeds | `hash_hit=False`, `top_similarity=None` (no active chunks in vault, or none within the coarse floor) | verdict is `PROCEED` | deterministic check | 100% |
| Disabled admission is a no-op (guardrail) | `memory.admission.enabled=False` on an input that would otherwise SKIP (`hash_hit=True`) | gate is never invoked; ingest behaves exactly as today (proceeds to extraction); no embedding computed, no admission query issued | deterministic check | 100% |
| Threshold boundary is settled by Q2, not silently drifted (guardrail; depends on Q2 → default 0.92, normalized) | `top_similarity` exactly equal to the configured `threshold` (0.92) | verdict is `SKIP` (comparison is `>=`, against the anisotropy-normalized similarity, per ticket Req 4 / Q2) | deterministic check | 100% |
| Near-dup lookup never leaks across vaults (guardrail) | An existing note in vault B whose content is identical to the incoming note targeting vault A; vault A has no similar content | verdict is `PROCEED` for vault A (the query is scoped to the target vault only, per Req 4) | deterministic check | 100% |
| Gate fails open on lookup error, never falsely skips (guardrail; depends on Q1/Q3 lookup impl) | the near-dup query raises (e.g. pgvector/session error) mid-check | verdict is `PROCEED` (fail-open, mirroring `_detect_overlapping_notes` non-fatal try/except at ingestion.py:1193); an error is logged; content is NOT skipped | deterministic check | 100% |
