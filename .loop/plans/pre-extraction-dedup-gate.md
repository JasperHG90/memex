# pre-extraction-dedup-gate: skip redundant content before the extraction LLM call

## 1. Title

Add a pre-extraction admission check (RFC #210 "A1") that scores
incoming note content against existing memory via (a) an exact
content-hash lookup and (b) an embedding near-duplicate lookup, and
**skips** the whole extraction pipeline on a hit — before any LLM call
runs. This is the FIRST stage of a two-stage admission gate; the seam is
built so RFC #155's surprise/novelty stage can slot in behind it without
rework. Disabled by default (`memory.admission.enabled = false`).

## 2. Size / Effort

**S–M.** The mechanism is small: one new `admission/` module (a pure
decision function plus a DB-querying gate), one new config block, and one
wiring call in `IngestionService.ingest`. No schema change, no
MCP/DTO/HTTP signature change (the gate is config-driven, not
parameter-driven). Effort is driven by the precision bar (§9) — a
false-positive skip silently drops genuinely new content — which forces a
conservative threshold, an offline-testable pure decision seam, and a
positive/negative scenario matrix. The embedding near-dup SQL and the
anisotropy normalization already exist as patterns to copy, not invent.

## 3. Triggered by

Triage decision to implement ONLY sub-item A1 of RFC #210 (Memory
Admission Control), scoped to the redundancy check with two signals —
content-hash (exact) and embedding similarity (near-duplicate) — that
short-circuits extraction on a hit. Source: `.temp/issues/rfc-210.md`
(section "A1: Pre-Extraction Redundancy Gate") and `gh issue view 210`.
Capacity management (A2) and the audit trail (A3) are explicitly out of
scope for this ticket.

## 4. Context

**Today ingestion is unconditional past two narrow guards.** The note
write path is `IngestionService.ingest`
(`packages/core/src/memex_core/services/ingestion.py:385`):

1. **Two-gate idempotency check**
   (`packages/core/src/memex_core/services/ingestion.py:412-443`). Gate 1:
   does a note with `id == note_uuid` exist, where `note_uuid =
   note.idempotency_key` (`packages/core/src/memex_core/api.py:264-269`,
   which delegates to `note_key`). Gate 2: does that row's stored
   `content_hash` equal `note.content_fingerprint`
   (`packages/core/src/memex_core/api.py:245-261`)? If both match →
   `return {'status': 'skipped', 'reason': 'idempotency_check'}`
   (`packages/core/src/memex_core/services/ingestion.py:431`). **This is
   IDENTITY-keyed**: it only skips a re-ingest of the SAME `note_key` with
   the SAME content. Identical content submitted under a *different*
   `note_key` / title / source sails straight through.

2. **Extraction runs** via `self.memory.retain(...)`
   (`packages/core/src/memex_core/services/ingestion.py:495`) — the
   expensive multi-call LLM extraction. Everything above line 495 (the
   transaction open at `:449`, file staging at `:454`, title/date LLM
   resolution at `:462-472`) has already happened by then.

3. **Post-extraction per-fact dedup** runs INSIDE `retain`:
   `deduplication.check_duplicates_batch`
   (`packages/core/src/memex_core/memory/extraction/deduplication.py:20`)
   → `check_duplicates_in_window`
   (`packages/core/src/memex_core/memory/extraction/storage.py:392`),
   called from the extraction engine
   (`packages/core/src/memex_core/memory/extraction/engine.py:419` and
   four sibling sites). It dedups individual **extracted facts**
   (`ProcessedFact`, each already carrying `.embedding`) by exact text +
   cosine similarity within a 24h window, vault-scoped. It fires only
   AFTER the LLM extraction has already been paid for.

4. **Informational note-overlap detection** runs AFTER `retain`:
   `_detect_overlapping_notes`
   (`packages/core/src/memex_core/services/ingestion.py:1137`, called at
   `:508`). It computes chunk-level cosine similarity between the
   just-ingested note and existing notes and returns `overlapping_notes`
   in the result. It **does not gate** anything — purely advisory — and it
   needs the note's chunks to already exist, i.e. post-extraction.

**What A1 adds, and how it extends rather than duplicates the above.**
A1 is a NEW pre-extraction admission stage that sits between the
idempotency check (`:443`) and the transaction open (`:449`). It adds two
lookups the existing guards do not perform:

- **Exact content, any identity.** The idempotency gate-2 already
  compares `note.content_fingerprint` — but only against the ONE row
  whose id is `note_uuid`. `Note.content_hash` is populated from exactly
  this fingerprint at store time
  (`packages/core/src/memex_core/memory/extraction/storage.py:146-149`:
  `content_hash = content_fingerprint`), and the column is indexed
  (`idx_notes_content_hash`,
  `packages/core/src/memex_core/memory/sql_models.py:291-294`, `:384`).
  A1 **widens** the same fingerprint comparison from `WHERE id =
  note_uuid` to `WHERE content_hash = note.content_fingerprint AND
  vault_id = target AND status = 'active'` (any note id). This catches the
  identical-content-under-a-different-key case the idempotency check
  structurally cannot. It reuses the fingerprint that is *already
  computed* — no new hashing.

- **Near-duplicate content.** Neither guard catches a paraphrase or a
  lightly-edited re-submission. A1 embeds the raw incoming content ONCE
  and runs a nearest-neighbour cosine lookup against existing active chunk
  embeddings in the vault, mirroring the pgvector SQL in
  `_detect_overlapping_notes`
  (`packages/core/src/memex_core/services/ingestion.py:1137-1195`) and the
  anisotropy normalization in `check_duplicates_in_window`
  (`packages/core/src/memex_core/memory/extraction/storage.py:461-499`,
  `get_shared_corrector().normalize(...)`). If the top normalized
  similarity ≥ `redundancy_threshold` (default 0.92 per the RFC) → skip.

The existing post-extraction per-fact dedup is NOT replaced: A1 prevents
the whole extraction from running when the ENTIRE incoming content is
redundant; the per-fact dedup continues to handle partial fact-level
overlap for content that DID pass admission. They are complementary
layers at different granularities and different pipeline stages.

**Embedding-reuse finding (asked in triage).** There is NO free embedding
available before extraction today. Fact embeddings are computed per
extracted fact, on fact text, INSIDE `retain` (post-LLM) —
`ExtractionEngine.embedding_model`
(`packages/core/src/memex_core/memory/extraction/engine.py:220`), applied
at `engine.py:404,416,...`. So A1's single raw-content embedding is
genuinely additional work — but it is one embedding versus a full
multi-call LLM extraction, which is the RFC's whole cost argument. The
embedder is reachable without new constructor plumbing via the cached
factory `get_embedding_model()`
(`packages/core/src/memex_core/memory/models/embedding.py:25`) and the
single-text helper `generate_embedding(backend, text)`
(`packages/core/src/memex_core/memory/extraction/embedding_processor.py:55`) —
the same factory `MemexAPI` uses at `api.py:445`.

**The #155 seam (design constraint from triage).** RFC #210 A1's decision
tree ends "Novel → Proceed to #155's surprise gate for depth routing." So
A1 must not return a bare boolean. It returns an `AdmissionDecision`
carrying the verdict (SKIP / PROCEED), the evidence (top similarity,
matched note id, hash-hit flag), AND **the raw-content embedding it just
computed**, so a future #155 novelty stage can be inserted immediately
after the dedup verdict and reuse that embedding instead of recomputing
it. The pure verdict logic is separated from the DB queries so both A1
now and #155 later can be exercised offline.

## 5. Non-goals / out of scope

- **A2 (capacity budget / throttling / congestion / `vault_capacity_config`
  table / `memory_vault_status` tool).** Not in this ticket.
- **A3 (audit trail / `admission_audit` table / `memory_admission_log`
  tool).** Not in this ticket. Admission decisions are logged via the
  existing logger + the skip return payload only; no new table.
- **The FTS / tsvector overlap signal** listed as a third A1 signal in the
  RFC. Triage scoped A1 to exactly two signals: content-hash and
  embedding similarity. Do NOT add the keyword-overlap check.
- **Delta extraction** (the RFC's "Redundant + new source → extract only
  novel portions" outcome). This ticket is binary **skip / proceed**
  only; delta-extraction touches the extraction pipeline and is deferred
  (see Q4).
- **Any schema change / migration.** The gate uses only existing columns
  and indexes (`Note.content_hash`, `chunks.embedding`).
- **Any MCP tool, `NoteCreateDTO`, HTTP endpoint, or `MemexAPI.ingest`
  signature change.** The gate is config-driven; nothing threads through
  the call layers. Do NOT add an override parameter to the tool surface.
- **Gating `ingest_from_url` / `ingest_from_file` / `append_to_note` /
  the batch path** (`packages/core/src/memex_core/services/ingestion.py:210`,
  `:279`, `:534`, `:926`). This ticket gates the single-note raw-content
  ingest path only (see Q3).
- **Changing the existing idempotency check or the post-extraction per-fact
  dedup.** A1 is added in front; those stay as-is.

## 6. Requirements & restrictions

Requirements:

1. When `memory.admission.enabled` is true AND the incoming content is
   judged redundant (exact content-hash hit OR near-duplicate embedding
   hit ≥ threshold), `IngestionService.ingest` MUST return a skip result
   (`{'status': 'skipped', 'reason': 'admission_redundant', ...evidence}`)
   **before** opening the transaction at
   `packages/core/src/memex_core/services/ingestion.py:449` — so a skipped
   note stages no files, writes no rows, and makes NO LLM call.
2. When `memory.admission.enabled` is false (the default), the ingest path
   MUST behave exactly as today — the gate is a no-op that adds no query
   and no embedding cost.
3. The admission check MUST run AFTER the existing idempotency check
   (`:412-443`) so an unchanged re-ingest still short-circuits via the
   cheaper identity path first, and the near-dup embedding is only
   computed for content that passed idempotency.
4. Detection MUST be conservative (see §9 precision bar). The default
   `redundancy_threshold` is 0.92 (RFC), the near-dup similarity MUST be
   anisotropy-normalized to match the existing per-fact dedup semantics
   (`storage.py:471,497`), and the near-dup lookup MUST be vault-scoped to
   the target vault (never cross-vault).
5. The gate MUST return an `AdmissionDecision` object (verdict + evidence
   + computed raw-content embedding), not a bare bool, so RFC #155 can
   insert a novelty stage behind the dedup verdict and reuse the
   embedding (§4 seam). The pure verdict function (signals →
   verdict) MUST be importable and callable with no DB and no model.

Restrictions (repo principles, each cited):

- **Simplicity / no speculative infra** (`CLAUDE.md` §2): one module, one
  config block, one call site. Do NOT build A2/A3 scaffolding "while we're
  here." The `#155` seam is a return-shape decision, not a second
  subsystem.
- **Surgical changes** (`CLAUDE.md` §3): touch only the new `admission/`
  module, the config, and the one guard call in `ingestion.py`. Do not
  refactor the surrounding ingest flow or the existing dedup code.
- **Every code change ships a test; reproduce first** (`.claude/rules/
  python-testing.md`, constraint `all-code-needs-tests`).
- **Tests are real code; gates must pass; no `# type: ignore` / `skip` /
  `xfail` to go green** (`.claude/rules/python-testing.md`, constraint
  `tests-are-real-code`; `.claude/rules/pre-existing-issues.md`).
- **Don't mock what you can run** (`.claude/rules/python-testing.md`,
  constraint `dont-mock-what-you-can-run`): the pure verdict function is
  tested directly with real values; the DB-backed gate, if tested, uses
  the Postgres testcontainer, not a mocked metastore.
- **Config is hierarchical Pydantic** — model `AdmissionConfig` on the
  existing sub-configs (`ExtractionConfig`
  `packages/common/src/memex_common/config.py:770`, its kill-switch
  `intent_risk_classifier_enabled` at `:797`) and attach it to
  `MemoryConfig` (`config.py:2383`, alongside `extraction` at `:2410`).
- **Adversarial review before done** (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

- `packages/core/src/memex_core/memory/admission/__init__.py` (NEW):
  package init; export the gate + decision types.
- `packages/core/src/memex_core/memory/admission/decision.py` (NEW):
  `AdmissionVerdict` enum — **strictly binary, `SKIP` | `PROCEED`** (the
  admission axis: does this content enter at all?) — and an
  `AdmissionDecision` dataclass (`verdict`, `reason`, `top_similarity:
  float | None`, `matched_note_id: UUID | None`, `hash_hit: bool`,
  `embedding: list[float] | None`). Plus the PURE verdict function, e.g.
  `decide(hash_hit: bool, top_similarity: float | None, threshold: float)
  -> AdmissionVerdict` — deterministic, no DB, no model. This is the
  offline-testable seam. NOTE (joint decision with #155, see Q4): extraction
  gradations are a SEPARATE axis from the verdict. #210 does NOT add a
  `DELTA` verdict member and does NOT add a depth field; #155's ticket adds
  an orthogonal `depth` field to this dataclass for its novelty-gated
  routing, consumed only when `verdict == PROCEED`. Keep the enum binary so
  the shared contract stays a clean admit/skip decision.
- `packages/core/src/memex_core/memory/admission/gate.py` (NEW): the
  DB-querying orchestrator, e.g. `async def check_admission(session,
  content_text, content_fingerprint, vault_id, config) ->
  AdmissionDecision`. It (1) runs the exact content-hash lookup (`SELECT
  Note.id WHERE content_hash = :fp AND vault_id = :v AND status =
  'active' LIMIT 1`), (2) on miss, embeds `content_text` once via
  `generate_embedding(await get_embedding_model(), content_text)` and runs
  the vault-scoped nearest-neighbour chunk-embedding cosine query
  (pattern from `_detect_overlapping_notes`,
  `packages/core/src/memex_core/services/ingestion.py:1137-1195`;
  normalize with `get_shared_corrector().normalize(...)` as in
  `packages/core/src/memex_core/memory/extraction/storage.py:461-499`),
  (3) calls the pure `decide(...)`, and (4) attaches the computed
  embedding to the returned `AdmissionDecision` for the #155 seam.
- `packages/common/src/memex_common/config.py:770` (context anchor —
  `ExtractionConfig`): ADD `class AdmissionConfig(BaseModel)` nearby with
  `enabled: bool = False`, `redundancy_threshold: float = 0.92` (0..1),
  and `near_dup_candidate_limit: int` (top-N chunks to normalize, mirror
  the `LIMIT 5` in `check_duplicates_in_window`). Default-off per RFC
  ship-safety.
- `packages/common/src/memex_common/config.py:2410` (context anchor — the
  `extraction:` field on `MemoryConfig`, class at `:2383`): ADD
  `admission: AdmissionConfig = Field(default_factory=AdmissionConfig,
  ...)`. Access path becomes `self.config.server.memory.admission`
  (matching `self.config.server.memory.extraction` at
  `packages/core/src/memex_core/api.py:445`).
- `packages/core/src/memex_core/services/ingestion.py:443` (immediately
  after the idempotency-check block, before the transaction open at
  `:449`): CALL `check_admission(...)` when
  `self.config.server.memory.admission.enabled`; on `SKIP`, `return
  {'status': 'skipped', 'reason': 'admission_redundant', 'evidence':
  {...}}` mirroring the idempotency skip return at `:431`. The session for
  the lookup can reuse the `async with self.metastore.session()` block
  already open at `:413` (extend it) or open its own — implementer's call,
  but do NOT open the `AsyncTransaction`.
- **Test files (loop-gating, REQUIRED in root `tests/`):**
  `tests/test_admission_decision.py` (NEW) — the offline pure-verdict
  test; `tests/test_admission_gate_integration.py` (NEW, `integration`) —
  the DB-backed gate test (does not run in the default loop gate; see §8).

## 8. Tests & validation gates

**Eval marker (acceptance layer) — MANDATORY, AUTHORED.** `.loop/config.json`
sets `require_eval: true`. The marker exists and validates (`loopctl eval
pre-extraction-dedup-gate` → `valid`):
`.loop/evals/pre-extraction-dedup-gate.md`. It holds 8 deterministic
scenarios over the pure `decide(...)` seam and the enabled/vault-scope/
fail-open invariants: exact hash-hit → SKIP; similarity ≥ threshold →
SKIP; similarity just below threshold → PROCEED (precision guard); no
hash-hit + no candidates → PROCEED; `enabled=false` → no-op; threshold
boundary (`>=`, normalized) → SKIP; cross-vault identical content →
PROCEED (no leak); lookup error → PROCEED (fail-open). Rows whose
assertion depends on an unsettled open question encode the recommended
default and name the dependency (Q2 threshold, Q1/Q3 lookup impl); if the
operator settles a fork differently, update the matching row.

Repo gates (verified this session):

- `just test` → `uv run pytest tests` (justfile `test:` recipe,
  `justfile:65-66`). Collects ONLY the root `./tests/` directory;
  `packages/*/tests/` are NOT collected by this loop gate.
- `addopts = "--timeout=300 --timeout-method=thread -m 'not integration'"`
  (`pyproject.toml:78`) — `integration`-marked tests are excluded from the
  default run. A pure-function test taking no DB fixture runs offline
  without Docker.
- `just prek` → `uv run prek run -a` (`justfile:61-62`) — ruff + mypy +
  configured hooks per `.pre-commit-config.yaml`. Tests are linted and
  type-checked like source.

Tests to add:

1. **`tests/test_admission_decision.py` (loop-gating, offline).** A
   parametrized unit test over the pure `decide(...)` verdict function.
   Positive (must SKIP): `hash_hit=True`; `top_similarity` above
   threshold. Negative — the precision guard (must PROCEED):
   `top_similarity` just below threshold; `top_similarity=None` (no
   candidates); a value equal to a *loosened* threshold but below the
   configured one. Pure function, no DB, no model, no mocks (per
   `dont-mock-what-you-can-run`). This is the authoritative loop-gated
   test because it lives in root `tests/`.
2. **`tests/test_admission_gate_integration.py` (marked `integration`).**
   Against the Postgres testcontainer: ingest a note with `admission.enabled=
   true`; re-ingest identical content under a DIFFERENT `note_key` → assert
   the second ingest returns `status='skipped', reason='admission_redundant'`
   and that NO second note/units were written. Ingest a genuinely novel
   note → assert it proceeds. Ingest a near-paraphrase → assert skip. Also
   assert `enabled=false` proceeds in every case. This proves the real
   pgvector query + the fingerprint-lookup widening, but because it is
   `integration` it does NOT run in the `just test` loop gate — it cannot
   be the sole test for the behaviour, which is why the pure-verdict test
   (1) carries the loop gate.

Mirror-source note: `.claude/rules/python-testing.md` says tests mirror
the source tree (→ `packages/core/tests/`). The loop gate collects only
root `./tests/`, so the authoritative offline test lives there; a mirrored
copy under `packages/core/tests/` is permitted but not required.

## 9. Risk assessment

- **Blast radius.** Every single-note ingest (MCP `add_note`, HTTP, CLI)
  flows through `IngestionService.ingest`, so the gate sits on the primary
  write path. Mitigated hard by the default-off flag (Req 2): with
  `enabled=false` the path is byte-for-byte today's behaviour.
- **Precision bar (governing constraint).** A false-positive skip
  *silently drops genuinely new content* — worse than the redundant-work
  status quo, and with A3 out of scope there is no audit trail to recover
  it from. Bias hard toward false-negatives (admit a borderline dup) over
  false-positives (skip a real note). Keep the threshold conservative
  (0.92), normalize similarity as the existing dedup does, and let the §8
  negative corpus be the enforcement mechanism. Because it ships
  default-off, real-world exposure is opt-in.
- **Reversibility.** High. Flip `admission.enabled` to false, or revert
  the one guard call. No migration, no data mutation, no deletion.
- **Likeliest failure modes.** (a) Threshold too aggressive → dropped
  content (mitigated by conservative default + default-off + negatives).
  (b) Granularity mismatch: a whole-content embedding compared to
  passage-level chunk embeddings can misfire on long notes (see Q1). (c)
  Guard placed after the transaction/extraction, so a skipped note still
  stages files or writes partial rows — mitigated by Req 1 (before `:449`).
  (d) Cross-vault leakage if the near-dup query forgets vault scoping —
  mitigated by Req 4. (e) The near-dup query raising instead of degrading
  — copy the non-fatal try/except posture of `_detect_overlapping_notes`
  (`ingestion.py:1193`): on query error, PROCEED (fail-open), never skip.

## 10. Subtickets

Ordered, dependency-aware:

1. Add `admission/decision.py` (the `AdmissionVerdict` enum,
   `AdmissionDecision` dataclass, pure `decide(...)`) + its offline gating
   test `tests/test_admission_decision.py` (red → green). Land the
   precision-guard negatives FIRST. (Depends on Q2.)
2. Add `AdmissionConfig` (`config.py:770`) and attach it to `MemoryConfig`
   (`config.py:2410`); default-off. Add a config-defaults assertion to an
   existing config test if cheap.
3. Add `admission/gate.py`: the exact content-hash lookup + the
   vault-scoped near-dup embedding query (copy the SQL/normalization
   patterns), returning `AdmissionDecision` with the embedding attached.
   (Depends on 1, 2, Q1, Q3.)
4. Wire `check_admission(...)` into `IngestionService.ingest` after the
   idempotency block and before the transaction open, honouring the flag;
   return the `admission_redundant` skip. Add
   `tests/test_admission_gate_integration.py` (`integration`). (Depends on
   1, 2, 3.)
5. Run `just test` and `just prek`; adversarial review per
   `.claude/rules/adversarial-reviews.md`.

## 11. Open questions

**Q1 — Near-dup comparison target and granularity: chunk embeddings vs
memory-unit embeddings, and how to embed long raw content?**
The exact-content check is unambiguous (fingerprint vs `Note.content_hash`).
The near-dup check has a granularity fork: compare the raw-content
embedding against (a) active **chunk** embeddings (passage-level, matches
`_detect_overlapping_notes`) or (b) **MemoryUnit** embeddings (fact-level,
matches `check_duplicates_in_window`). A single whole-note embedding vs
either is granularity-mismatched for long content.
*Recommendation:* **(a) chunk embeddings**, and for long content embed a
truncated/leading-window representation rather than the whole body (a
whole-document embedding washes out). Document the granularity caveat in
the module docstring and the suite/README. **Operator confirms the
comparison target.**

**Q2 — Threshold value and whether it is anisotropy-normalized.**
The RFC states 0.92. The existing per-fact dedup normalizes similarity via
the anisotropy corrector before thresholding (`storage.py:471,497`).
*Recommendation:* default `redundancy_threshold = 0.92`, compared against
the **normalized** similarity (consistent with existing dedup semantics),
configurable. **Operator sets the risk appetite / confirms normalization.**

**Q3 — Which surfaces does the gate cover?**
Options: (a) single-note `IngestionService.ingest` only; (b) also
`ingest_from_url` / `ingest_from_file`; (c) also the batch path.
*Recommendation:* **(a) only.** URL/file capture of an external document
that overlaps existing memory is a different intent (deliberate archival),
and the batch path has its own dedup invariant (`processing/batch.py`).
Gate the one agent/user note path this RFC targets; widen later if needed.
**Operator decides scope.**

**Q4 — Binary skip/proceed vs the RFC's three-way (skip / delta-extract /
proceed)? — RESOLVED (joint decision with #155).**
The RFC's "Redundant + new source → delta-extract novel portions" outcome
is materially more complex (it reaches into the extraction pipeline to
extract only novel spans) and risks touching the "extraction pipeline
untouched" ship-safety guarantee.
*Resolution:* **verdict stays strictly binary (`SKIP` | `PROCEED`);
delta-extraction is deferred to a follow-up.** Extraction gradations are
modeled as a SEPARATE axis, NOT as verdict members — decided jointly with
the #155 surprise-gate ticket, which adds an orthogonal `depth` field to
`AdmissionDecision` for its novelty routing (consumed only on `PROCEED`). A
future #210 delta-extraction, if built, expresses itself on that same depth
axis (or its own mode field), never by expanding the verdict enum. This
keeps the shared admission contract a clean admit/skip decision and avoids
a combinatorial verdict enum. Triage already scoped A1 to "skip or
short-circuit."

**Q5 — How does the gate obtain the embedder — cached factory vs injected
handle?**
`IngestionService.__init__`
(`packages/core/src/memex_core/services/ingestion.py:188-205`) holds
`metastore/filestore/config/lm/memory/vaults` but no embedder. `MemexAPI`
reaches the model via the cached `get_embedding_model()` factory
(`api.py:445`, `memory/models/embedding.py:25`).
*Recommendation:* **call `get_embedding_model()` inside the gate** (it is
cached; no new constructor plumbing, keeps the change surgical). Inject
only if a test needs to substitute the model — and prefer the real ONNX
model over a mock per `dont-mock-what-you-can-run`. **Operator approves.**
