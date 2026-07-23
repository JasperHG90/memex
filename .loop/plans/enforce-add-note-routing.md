# enforce-add-note-routing: reject how-to-shaped bodies at the add_note boundary

## 1. Title

Enforce the note-vs-case routing rule structurally: when `add_note`
receives a body that is clearly a how-to / procedure / worked-episode
(the shape that belongs in the procedural plane via `case_submit`),
reject it with a 4xx whose message points the caller to
`memex_case_submit`, instead of silently ingesting a how-to as a note
where it becomes invisible to the procedural plane.

## 2. Size / Effort

**M.** The change itself is a single pre-persist guard plus one new pure
detector function and one exception type. Effort is driven not by line
count but by the precision bar (§9): the detector must almost never
reject a legitimate declarative note, and that constraint forces a
conservative, multi-signal heuristic and a thorough parametrized test
matrix of both positive (how-to) and negative (declarative-with-steps)
corpora. The override plumbing threads one boolean through three layers
(MCP tool → DTO → service signature).

## 3. Triggered by

Operator request: move the single most-emphasized routing rule in the
host doctrine from agent-read PROSE into an enforced structural check.
The doctrine states the failure directly:
`.claude/rules/memex-agent-surface.md:136` —
"a how-to saved as a note is invisible to the procedural plane — the #1
mistake" (constraint `procedural_vs_semantic_add`, declared at
`.claude/rules/memex-agent-surface.md:134`). The MCP tool description
already tells the agent "those go to memex_case_submit, NEVER here"
(`packages/mcp/src/memex_mcp/server.py:993-995`), but nothing enforces
it — a mis-routed how-to is accepted and silently mis-filed.

## 4. Context

Today the `add_note` write path is:

- MCP tool `memex_add_note` builds a `NoteCreateDTO` and calls
  `api.ingest(...)` — `packages/mcp/src/memex_mcp/server.py:1007`
  (decorator `989`), DTO construction `1169-1182`, ingest call
  `packages/mcp/src/memex_mcp/server.py:1184`.
- HTTP endpoint `POST /ingestions/note` decodes the DTO and calls
  `api.ingest(...)` — `packages/core/src/memex_core/server/ingestion.py:157`,
  ingest call at `:216`.
- `MemexAPI.ingest` validates intent/risk overrides and delegates —
  `packages/core/src/memex_core/api.py:1173`, override validation
  `:1199-1219`, delegation `:1221`.
- `IngestionService.ingest` runs the transactional pipeline —
  `packages/core/src/memex_core/services/ingestion.py:385`. The body is
  decoded to `content_text` at `:460`, then fact extraction runs inside
  the transaction via `self.memory.retain(...)` at `:495`.

There is already a **write-time classifier** precedent, but it operates
per-extracted-fact (intent_class / risk_class on `MemoryUnit`), NOT on
the note body:

- `MemoryUnit.intent_class` / `risk_class` columns —
  `packages/core/src/memex_core/memory/sql_models.py:729` and `:735`.
- The safety filter is the closest structural precedent for a
  content-shaped policy decision at write time:
  `filter_safety_blocked` at
  `packages/core/src/memex_core/memory/extraction/classifier.py:33`. Note
  its docstring (`classifier.py:3-6`) explicitly says safety facts are
  "recorded but passed through … blocking will be handled by a future
  pre-flight risk assessment." So the classifier does NOT currently
  block; it counts. The how-to-shape check is a genuinely new pre-persist
  *rejection*, not an extension of existing classifier behaviour.

The routing target the error must name:

- `memex_case_submit` tool — `packages/mcp/src/memex_mcp/server.py:4840`;
  it files a `role='case'` note into the hidden procedural vault
  (`api.case_submit` at `packages/mcp/src/memex_mcp/server.py:4872`,
  service `packages/core/src/memex_core/services/case_service.py:117`).
- `Note.role` provenance field documents the procedural-plane roles
  (`case` / `procedure` / `strategy`) —
  `packages/core/src/memex_core/memory/sql_models.py:361`.

What is wrong / missing: no code path inspects the `add_note` body for
procedural shape. A how-to submitted to `add_note` is accepted, extracted
as declarative facts, and never reaches the procedural derivation
pipeline — exactly the "#1 mistake" the doctrine warns against.

## 5. Non-goals / out of scope

- Do NOT change `case_submit`, the procedural plane, case derivation, or
  retrieval.
- Do NOT auto-route: do not silently redirect an `add_note` into a
  `case_submit`. The contract is REJECT-and-instruct. Auto-routing hides
  the decision from the caller. (See Open Question Q5 for the alternative
  and why reject is the recommended default.)
- Do NOT touch the existing intent/risk classifiers' behaviour
  (`classifier.py:33`, the `MemoryUnit.intent_class` / `risk_class`
  columns). This ticket adds a body-level gate, not a per-fact one.
- Do NOT gate `ingest_from_url` / `ingest_from_file`
  (`packages/core/src/memex_core/services/ingestion.py:210`, `:279`):
  external document capture (a PDF/URL that happens to contain
  instructions) is legitimate declarative capture and is not the
  agent-mis-routing failure this targets. (Confirm in Q2.)
- Do NOT add an LLM call to the write path unless Q1 is decided against
  the heuristic recommendation.

## 6. Requirements & restrictions

Requirements:

1. When an `add_note` body is detected as how-to-shaped AND the caller
   has not set the override, `add_note` MUST fail with a 4xx whose
   message names `memex_case_submit` as the correct destination and names
   the override that bypasses the check.
2. The check MUST run **before** persistence and before fact extraction
   (before `self.memory.retain(...)` at
   `packages/core/src/memex_core/services/ingestion.py:495`), so a
   rejected note leaves no rows and no filestore artifacts.
3. An explicit override parameter MUST let a caller who really means it
   bypass the check (the escape hatch that keeps a false positive from
   being a hard wall). See §7 and Q3.
4. Detection MUST be conservative: the precision bar in §9 governs. A
   false-positive rejection of a real note is worse than the status quo.

Restrictions (repo principles, each cited):

- **Simplicity / no speculative infra** (`CLAUDE.md` §2, "Minimum code
  that solves the problem"): prefer the heuristic (Q1) and a single guard
  call site over a parallel classification pipeline.
- **Surgical changes** (`CLAUDE.md` §3): touch only the ingest guard, the
  detector module, the exception, and the override plumbing. Do not
  refactor the surrounding ingest flow.
- **Every change ships a test; bug/feature reproduced by a test first**
  (`.claude/rules/python-testing.md`, constraint `all-code-needs-tests`).
- **Tests are real code, gates must pass, no `# type: ignore` /
  `skip` / `xfail` to go green** (`.claude/rules/python-testing.md`,
  constraint `tests-are-real-code`; `.claude/rules/pre-existing-issues.md`).
- **Don't mock what you can run** (`.claude/rules/python-testing.md`,
  constraint `dont-mock-what-you-can-run`): the detector is a pure
  function — test it directly with real strings, no mocks.
- **Dependencies via `uv add`** if any are needed
  (`.claude/rules/uv-installer.md`) — none expected for the heuristic.
- **Adversarial review before done** (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

- `packages/core/src/memex_core/memory/extraction/procedural_shape.py`
  (NEW): a pure detector, e.g.
  `is_procedural_shape(text: str) -> bool` (or a scored variant returning
  a signal breakdown). Multi-signal, deterministic, offline, no LLM. Lives
  beside the existing write-time classifier module
  (`extraction/classifier.py`) so the two write-time policy checks sit
  together. Detection signals to combine (Q4 sets the threshold):
  ordered imperative step lists, the explicit
  `Trigger`/`Situation`/`Actions`/`Outcome` scaffold the case shape uses,
  numbered/bulleted procedural sequences dominating the body, imperative
  verb density.
- `packages/common/src/memex_common/exceptions.py:93` (context anchor —
  `DeltaValidationError`): ADD a new `MemexError` subclass (e.g.
  `ProceduralShapeRejectedError`) modelled on the caller-correctable
  precedents here. Whether it also subclasses `ValueError` (422) vs plain
  `MemexError` (400) is Q6.
- `packages/core/src/memex_core/server/common.py:99-131` (context anchor —
  the `_handle_error` isinstance ladder): ADD a mapping clause for the new
  exception to the chosen 4xx (see the 422 precedents at `:117`, `:128`
  and the generic `MemexError`→400 fallback at `:130`), so the HTTP
  surface returns the 4xx the contract requires rather than a 500.
- `packages/core/src/memex_core/services/ingestion.py:459` (just before
  the `content_text` decode / extraction block at `:460-495`): CALL the
  detector on `content_text`; when it fires and the override is unset,
  raise the new exception before opening no further work.
- `packages/core/src/memex_core/api.py:1173` (`MemexAPI.ingest`
  signature/body): THREAD the override parameter through to
  `self._ingestion.ingest(...)` at `:1221`, mirroring how
  `intent_override` / `risk_override` are already validated and forwarded
  (`:1199-1221`).
- `packages/common/src/memex_common/schemas.py:827` (`NoteCreateDTO`):
  ADD the override field (default `False`) so HTTP/MCP callers can set it,
  alongside the existing `intent_class` / `risk_class` / `template`
  fields. (Q3 decides the exact mechanism/name.)
- `packages/mcp/src/memex_mcp/server.py:1007` (`memex_add_note` params)
  and `:1169` (`NoteCreateDTO` construction): ADD the override parameter
  (default `False`) and pass it into the DTO.
- `packages/core/src/memex_core/server/ingestion.py:216` (the
  `api.ingest(...)` call inside `ingest_note`): FORWARD the DTO override
  into the ingest call, matching the existing `intent_override` /
  `risk_override` forwarding at `:218-220`.
- **Test file (loop-gating, REQUIRED in root):**
  `tests/test_procedural_shape_gate.py` (NEW) — the authoritative,
  offline unit test for the detector (see §8 for why it must live here).

## 8. Tests & validation gates

**Eval marker (acceptance layer):** `.loop/evals/enforce-add-note-routing.md`
— 6 deterministic scenarios at 100%. Guardrails: how-to rejected + names case_submit;
legitimate-note-with-steps accepted (precision); single-weak-signal not rejected.
Fork-dependent rows: status code (Q6→422), threshold (Q4→≥2 signals), override (Q3→`allow_procedural`).

Repo gates (verified this session):

- `just test` → `uv run pytest tests` (justfile `test:` recipe). Collects
  ONLY the root `./tests/` directory; `packages/*/tests/` are NOT
  collected by this loop gate.
- `addopts = "--timeout=300 --timeout-method=thread -m 'not integration'"`
  (`pyproject.toml:78`) — integration tests are excluded by default; the
  Postgres testcontainer is fixture-gated (`tests/conftest.py:121-125`,
  `postgres_container` scope=session), so a pure unit test that takes no
  DB fixture runs without Docker.
- `just prek` → `uv run prek run -a` (ruff + mypy + configured hooks per
  `.pre-commit-config.yaml`). Tests are linted and type-checked like
  source.

Tests to add:

1. **`tests/test_procedural_shape_gate.py` (loop-gating, offline):** a
   parametrized unit test over `is_procedural_shape`. It MUST assert BOTH
   directions:
   - **Positive (must reject):** a Trigger/Situation/Actions/Outcome
     worked-episode; a numbered "how we deploy" step list; an imperative
     runbook.
   - **Negative (must NOT reject) — the precision guard:** a decision
     record / ADR that *cites* a procedure; a fact with a single
     illustrative example; a note that merely contains one bulleted list;
     ordinary prose. These negatives are the load-bearing half — they
     encode the §9 precision bar as executable assertions.
   This test lives in root `tests/` (not `packages/core/tests/`) because
   only root `./tests/` is collected by the `just test` loop gate. It is a
   pure-function test (no DB, no network, no mocks per
   `dont-mock-what-you-can-run`).
2. **Guard-level test (optional, mirrored):** an offline test that the
   `IngestionService.ingest` guard raises the new exception when the
   detector fires and the override is unset, and does NOT raise when the
   override is set. If written against the live service it needs the DB
   fixture (integration); prefer asserting the guard logic without a full
   ingest where possible. If added as an integration/e2e test, mark it
   `integration` per `.claude/rules/python-testing.md` — but note it will
   then NOT run in the loop gate, so it cannot be the sole test for this
   behaviour.

Note on the mirror-source convention: `.claude/rules/python-testing.md`
says tests mirror the source tree (→ `packages/core/tests/...`). The loop
gate collects only root `./tests/`. The authoritative behaviour test for
this ticket therefore lives in root `tests/` so `just test` actually
gates it; a mirrored copy under `packages/core/tests/` is permitted but
not required and does not substitute for the root gate.

## 9. Risk assessment

- **Blast radius:** every `add_note` call (MCP, HTTP, CLI) passes through
  `IngestionService.ingest`, so the guard sits on the primary write path.
  A too-aggressive detector rejects legitimate notes — a user-facing
  regression worse than the silent-misfile status quo it replaces.
- **Precision bar (the governing constraint):** the detector must reject
  only bodies that are *predominantly* procedural. A note that merely
  contains steps, one list, or an example MUST pass. Bias hard toward
  false-negatives (let a borderline how-to through) over false-positives
  (block a real note). Require multiple co-occurring signals before
  firing; a single numbered list is not enough. The negative test corpus
  in §8 is the enforcement mechanism for this bar.
- **Reversibility:** high. The guard is one call site plus a default-off
  override; disabling it is a one-line revert. No schema migration (the
  override rides on the DTO / a nullable field, not a persisted column —
  confirm in Q3). No data is mutated or deleted.
- **Likeliest failure modes:** (a) false-positive rejection of a
  declarative note with incidental steps; (b) the override not threading
  cleanly through all three layers, leaving a caller unable to bypass;
  (c) placing the guard after extraction so a rejected note still writes
  partial rows — mitigated by Requirement 2 (guard before `retain` at
  `ingestion.py:495`); (d) the exception mapping to a 500 instead of a
  4xx if the `_handle_error` clause (`server/common.py`) is omitted.

## 10. Subtickets

Ordered, dependency-aware:

1. Add the pure detector `procedural_shape.py` + its offline gating test
   `tests/test_procedural_shape_gate.py` (red → green). Land the precision
   bar as the negative-corpus assertions FIRST. (Depends on Q1, Q4.)
2. Add the `ProceduralShapeRejectedError` exception and its
   `_handle_error` 4xx mapping in `server/common.py`. (Depends on Q6.)
3. Thread the override parameter: `NoteCreateDTO` field → `memex_add_note`
   param + DTO construction → `MemexAPI.ingest` → `IngestionService.ingest`
   → HTTP `ingest_note` forwarding. (Depends on Q3.)
4. Wire the guard call into `IngestionService.ingest` before extraction,
   honouring the override. (Depends on 1, 2, 3.)
5. Run `just test` and `just prek`; adversarial review per
   `.claude/rules/adversarial-reviews.md`.

## 11. Open questions

**Q1 — Detection method: heuristic vs LLM/DSPy classifier?**
The existing write-time classifier folds intent/risk into the LLM
extraction signature (`memex_core/metrics.py:440-463`,
`extraction/classifier.py`). Option (a): a cheap deterministic heuristic
(imperative-step structure, Trigger/Situation/Actions/Outcome scaffold,
numbered procedural steps). Option (b): an LLM/DSPy classifier like those
already used at write time.
*Recommendation:* **heuristic-first (a).** It keeps the check offline,
deterministic, and unit-testable under `just test` without Docker or a
model, and it keeps the write path fast. Escalate to an LLM only if the
heuristic cannot hit the precision bar. **Operator must confirm.**

**Q2 — Which surfaces does the guard cover?**
Options: (a) all raw-`NoteInput` ingest via `IngestionService.ingest`
(covers MCP `add_note` + HTTP + CLI); (b) MCP `memex_add_note` tool only;
(c) also gate `ingest_from_url` / `ingest_from_file`.
*Recommendation:* **(a).** Gating at `IngestionService.ingest:385` covers
every raw-note caller from one site; exclude URL/file ingestion (external
document capture is legitimately declarative and not the mis-routing
failure targeted). **Operator decides scope.**

**Q3 — Override mechanism and name?**
Options: (a) a new explicit boolean (e.g. `allow_procedural` / `force`)
threaded through the MCP tool → `NoteCreateDTO` → `ingest` signature (not
persisted); (b) a frontmatter flag in the body; (c) reuse `template`
(e.g. a reserved slug).
*Recommendation:* **(a), default `False`, not persisted** — explicit,
discoverable in the tool schema, and symmetric with the existing
`intent_class` / `risk_class` override plumbing
(`api.py:1199-1221`, `schemas.py` DTO fields). **Operator picks the
parameter name.**

**Q4 — Precision threshold / how many signals must co-occur?**
The detector must not fire on a single list or example.
*Recommendation:* require **≥2 independent co-occurring signals** (e.g.
ordered imperative steps AND either the Trigger/Situation/Actions/Outcome
scaffold or dominant-procedural-structure), tuned so the entire §8
negative corpus passes. Treat the negative corpus as the acceptance gate:
if any legitimate note is rejected, the threshold is wrong. **Operator
sets the risk appetite** (how conservative).

**Q5 — Reject-and-instruct vs auto-route?**
The request mandates reject-and-instruct and forbids silent
auto-routing.
*Recommendation:* **reject-and-instruct (confirmed).** Auto-routing an
`add_note` into a `case_submit` hides the decision and can mis-file on a
false positive with no caller signal; the loop's own
`unresolved-design-fork` philosophy favours surfacing the decision.
Alternative (auto-route) is recorded here only for completeness.
**Operator confirms the stance.**

**Q6 — Error status code / exception hierarchy: 400 vs 422?**
`_handle_error` maps generic `MemexError`→400 (`server/common.py:130`),
while `ValueError`-mixing errors like `DeltaValidationError`
(`exceptions.py:93`) and `ProceduralConstraintViolation` map to 422
(`server/common.py:117`, `:128`).
*Recommendation:* **422**, via a `ProceduralShapeRejectedError` that mixes
in `ValueError` (mirrors `DeltaValidationError`). 422 reads as
"understood but not processable as submitted; re-route or set the
override" — caller-correctable, matching the precedent. Either way the
message MUST name `memex_case_submit` and the override. **Operator
approves the status code.**
