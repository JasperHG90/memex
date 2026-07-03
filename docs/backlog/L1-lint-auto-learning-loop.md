# L1 — Lint auto-learning loop

**Status:** Phase 2 shipping in this PR; phases 3–5 tracked, not implemented.
**Branch:** `feat/maintenance-proposal-cockpit-tui`
**Plan file:** `~/.claude/plans/lint-auto-learning-loop.md`

## Problem

The maintenance linter is open-loop. Operator verdicts (accept / dismiss /
no_op + the chosen canned action + free-form note) land in
`evidence.resolution` thanks to the cockpit work, but nothing reads them
back. The same `llm_schema_drift` finding fires every sweep regardless of
how many times an operator dismissed it. Pressing through the cockpit
feels like rubber-stamping noise because the system's priors never
update from operator feedback.

## Layered architecture

| Layer | What it does | LLM cost | In this PR? |
|---|---|---|---|
| 1 — Capture | Verdicts land in `evidence.resolution` | none | ✅ shipped (cockpit PR) |
| **2 — Telemetry** | **Per-rule accept/dismiss rates, surfaced via CLI + cockpit** | **none** | **✅ ships now** |
| 3 — Threshold calibration | Per-rule emission thresholds tuned from verdicts | none | ❌ tracked |
| 4 — DSPy compile | LLM signatures retrained from labeled verdicts | weekly token spend | ❌ tracked |
| 5 — Auto-solve | High-confidence findings resolve themselves | opt-in | ❌ tracked |

The plan file at `~/.claude/plans/lint-auto-learning-loop.md` carries the
full architecture per phase, the table schemas, the invariants
(reversibility, cold-start safety, no silent regressions, no leakage),
and the six risks called out before any of this ships.

## Layer 2 — what is shipping in this PR

A read-only observability layer. No learning yet; just the visibility
that every later phase reads from.

**New table** (Alembic migration 047):

```sql
CREATE TABLE lint_rule_telemetry (
    rule_name        TEXT NOT NULL,
    vault_id         UUID,                  -- NULL = global aggregate
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    accept_count     INT NOT NULL,
    no_op_count      INT NOT NULL,
    dismiss_count    INT NOT NULL,
    legacy_count     INT NOT NULL,
    median_surprise  FLOAT,
    median_time_to_resolve_seconds INT,
    PRIMARY KEY (rule_name, vault_id, window_start)
);
```

**New service:** `LintLearningService` at
`packages/core/src/memex_core/services/lint_learning.py`.

- `refresh_telemetry(vault_id=None, window_days=30)` — reads
  `maintenance_proposals` for the trailing window, classifies each
  resolved row, upserts one row per `(rule_name, vault_id)`. Idempotent.
- `get_telemetry(rule_name=None, vault_id=None)` — returns DTOs ordered
  by `accept_rate ASC` (noisiest rules first).

**Verdict classification** (the heart of the rollup):

| Row state | Counter bumped |
|---|---|
| `status='resolved'` AND `evidence.resolution.followup.action` is set AND action_id NOT IN ('no_op',) | `accept_count` |
| `status='resolved'` AND `evidence.resolution.followup.action = 'no_op'` | `no_op_count` |
| `status='resolved'` AND no `resolution.followup` block (pre-cockpit row) | `legacy_count` |
| `status='dismissed'` | `dismiss_count` |
| `status='pending'` | ignored |

**HTTP:**

- `GET /api/v1/lint/calibration/telemetry?rule=<name>&vault_id=<uuid>` →
  list of telemetry rows.
- `POST /api/v1/lint/calibration/refresh?vault_id=<uuid>&window_days=30` →
  triggers `refresh_telemetry`. Gated by `require_write`. Returns counts.

**CLI:**

```
memex lint stats                                     # global table, all rules
memex lint stats --vault localstack                  # vault-scoped
memex lint stats --vault localstack --rule llm_schema_drift
memex lint stats refresh --vault localstack          # recompute on demand
```

The `stats` table renders: rule_name · accepts · no_ops · dismisses ·
accept_rate · legacy · window_end.

**Tests:**

- Unit test for the classification logic against synthetic finding
  dicts (no DB needed).
- Integration test (testcontainer Postgres) seeds proposals across two
  vaults with mixed verdicts and asserts the rollup matches.
- CLI test asserts table contents render.

**Out of scope for this PR:**

- Scheduler integration (nightly refresh). The endpoint exists; the
  scheduler hookup ships in the next slice.
- Cockpit detail-card augment ("accept_rate: 78% over 30d"). Ships next.
- Threshold calibration (Phase 3) — separate ticket.

## Why ship Phase 2 first

Three reasons:

1. **You cannot tune what you cannot see.** Phases 3–5 all read from
   `lint_rule_telemetry`. Without the table populated, none of them have
   ground truth to work against.
2. **It is the cheapest layer to verify.** No LLM cost; no ML; no
   reversibility concerns beyond "did the SQL aggregate correctly."
3. **It is independently useful.** Even if 3–5 never ship, a CLI that
   says "this rule is 90% dismiss, kill it" pays for itself.

## Phase 3+ tracker

The plan file at `~/.claude/plans/lint-auto-learning-loop.md` has the
full schemas + service contracts + cli surface for Phases 3, 4, 5. Each
is a separate ticket when the time comes. The open questions called out
there ("DSPy program serialisation format", "validation metric shape",
"cold-start under N", "rule-retirement signal") are blockers for Phase 4
design, not for shipping Phase 2.

## Definition of done (this PR)

- [ ] `just db-upgrade` applies migration 047.
- [ ] `memex lint stats refresh --vault <name>` returns a non-zero count
      on a vault that has resolved proposals.
- [ ] `memex lint stats --vault <name>` renders the rollup table.
- [ ] Unit + integration + CLI tests all green.
- [ ] Lint clean.
