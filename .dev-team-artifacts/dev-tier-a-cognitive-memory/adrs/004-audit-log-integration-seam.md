# ADR-004: Audit Log Is the Integration Seam Between F20 Revisitation and F38 Consolidation

## Status

Accepted

## Context

F20 (Revisitation / FSRS-driven review) and F38 (Consolidation tick) are two cognitive-memory features that collaborate: F20 records the outcome of a review against a memory unit (quality, scheduled-next interval), and F38 reads those outcomes to decide which units to consolidate, prune, or surface for follow-up.

A direct service-to-service import (`ConsolidationService` calls `RevisitationService`, or vice versa) would couple two independently-evolving features at the type and method-signature level. Each feature has its own RFC, its own owner, and its own iteration cadence. A coupled API would force lock-step releases and make either side hard to refactor without breaking the other.

We already have an append-only audit log that every state-changing operation in the system writes to. Reusing it as the integration seam is essentially free.

## Decision

F20's `RevisitationService.review()` emits `AuditLog(action='outcome.record', resource_type='memory_unit', resource_id=<unit_id>, payload=<quality, fsrs_state>)` per call.

F38's `ConsolidationService.tick()` reads these via `select_diff_units`, which filters on `AuditLog.action == 'outcome.record'` and returns the affected memory units since the last consolidation watermark. F38 does not import F20; F20 does not import F38.

Implemented in `packages/core/src/memex_core/services/revisitation.py` and `packages/core/src/memex_core/services/consolidation.py:select_diff_units`. Cross-feature contract is exercised by integration tests under `packages/core/tests/integration/`.

## Consequences

**Positive:**
- F20 and F38 evolve independently as long as the audit-log payload shape is preserved.
- The audit log is already durable and queryable — no new infra.
- Other features (telemetry, replay, debugging) get F20 outcomes for free via the same channel.
- Easy to add new consumers (e.g., a future "spaced learning report" feature) without touching F20 or F38.

**Negative:**
- The contract is a string literal (`'outcome.record'`) plus a payload schema, not a typed interface. A typo in either side fails silently. Mitigated by a constant in a shared module and an integration test that round-trips a real review through both services.
- Reading via `select_diff_units` requires a watermark — F38 must persist its last-seen audit cursor to avoid replaying outcomes.

## Alternatives Considered

- **Direct service import** — rejected: couples release cadence and makes either side hard to refactor.
- **Dedicated `revisit_outcomes` table** — rejected: duplicates information already captured in the audit log, and creates two write paths to keep in sync.
- **Event bus (e.g., NATS, Redis streams)** — rejected: adds infra; the audit log already provides at-least-once semantics with cursor-based replay.
