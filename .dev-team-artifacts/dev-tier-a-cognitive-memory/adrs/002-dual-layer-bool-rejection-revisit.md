# ADR-002: Dual-Layer `BeforeValidator` Rejects `bool` for Revisit Quality

## Status

Accepted

## Context

F20's `quality` parameter accepts a `Quality` enum value via either an integer (`0..3`) or a string label (`'again' | 'hard' | 'good' | 'easy'`). The natural Pydantic type is `int | str`. Python's type system treats `bool` as a subclass of `int`, so `True` and `False` are valid `int` values. Pydantic's union resolution coerces `bool` to `int` BEFORE the function body runs: `True → 1 → Quality.HARD`, `False → 0 → Quality.AGAIN`.

This is a silent corruption vector. A buggy caller passing `True` would receive HTTP 200, the audit log would record an "outcome.record" row, FSRS counters would update, and downstream consolidation (F38) would consume the bogus data — the system would "work" while quietly poisoning the per-unit retention model. A single-layer guard would leak the moment a new entry point bypassed it.

## Decision

Reject `bool` at the Pydantic validator gate using `BeforeValidator(_reject_bool_quality)` BEFORE int coercion. Apply this at BOTH entry points:

- MCP tool surface: `packages/mcp/src/memex_mcp/server.py`
- HTTP route surface: `packages/core/src/memex_core/server/revisit.py`

`_reject_bool_quality` raises a typed validation error (`'quality must be int 0-3 or string label, not bool'`) so callers get a 422 with an actionable message.

## Consequences

**Positive:**
- A buggy or malicious caller cannot smuggle `True/False` into the FSRS state machine.
- Defense-in-depth: a single-layer regression on one surface does not reopen the corruption vector on the other.
- The validator is reusable for any future field that takes `int | str` and must reject `bool`.

**Negative:**
- The same validator is wired in two places — both must be updated together if the contract changes. This is enforced by an integration test that exercises both surfaces with a `True` payload and asserts a 422.
- Slight cognitive overhead: contributors must understand WHY `bool` is rejected (the subclass relationship is not obvious).

## Alternatives Considered

- **Single-layer guard at the service** — rejected: any future entry point (CLI, gRPC, batch import) would have to remember to call the service, and a direct route handler bypassing the service would silently reintroduce the bug.
- **Use a `Literal[0,1,2,3] | Literal['again','hard','good','easy']` type instead of `int | str`** — rejected: `Literal[0,1,2,3]` still accepts `True/False` because `True == 1` and `False == 0` under Python equality, which Pydantic uses for `Literal` matching.
- **Strict mode globally** — rejected: too disruptive; would require auditing every union in the codebase.
